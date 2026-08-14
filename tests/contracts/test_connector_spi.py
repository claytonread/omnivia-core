"""V06-8 packet A6-N1: the four-operation SPI, its DTOs and the host checks.

These tests are mostly about refusal, for the same reason the runtime
foundation's are: a connector is code this repository did not write, and every
value it hands over ends up somewhere with a domain. What is new here is the
lineage digest, and the tests that matter most for it are the *omission
mutants* -- for each of the seven framed fields, a preimage that left it out
would let two different states hash alike. Those are the tests that say the
framing is load-bearing rather than decorative.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from omnivia_core.connector.fake import (
    FAKE_LIMITS,
    FakeSourceConnector,
    SourceScript,
    SourceWindow,
    synthetic_observations,
)
from omnivia_core.connector.host import (
    CURSOR_DIGEST_DOMAIN_TAG,
    ScopedCredentialResolver,
    admit_observation,
    canonical_cursor_digest,
    contains_known_material,
    cursor_digest_preimage,
    declared_encodings,
    metadata_depth,
    poll_context_surface_defects,
    run_cursor_migration,
    validate_batch,
    validate_migration,
    validate_registration,
    validate_successor,
)
from omnivia_core.connector.models import ConnectorContractError
from omnivia_core.connector.spi import (
    ERROR_CONNECTOR_CURSOR_FOREIGN,
    ERROR_CONNECTOR_CURSOR_NOT_MONOTONIC,
    ERROR_CONNECTOR_CURSOR_UNMIGRATABLE,
    ERROR_CONNECTOR_SECRET_EXPOSED,
    ERROR_CONNECTOR_STATE_INVALID,
    MAX_CURSOR_PAYLOAD_BYTES,
    SPI_OPERATIONS,
    Batch,
    ConnectorRefused,
    CredentialHandle,
    CursorBinding,
    CursorRecord,
    CursorState,
    Deadline,
    IdentityStability,
    Observation,
    PollContext,
    SourceConnectorSpi,
    SpiVersion,
    classify_payload,
)
from omnivia_core.contracts.v1.generated import (
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_DEADLINE_EXCEEDED,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
)

NOW_US = 1785000000000000
BINDING = CursorBinding(workspace_id="workspace-alpha", connector_id="fake.recordset")


def cursor(**overrides: object) -> CursorState:
    values: dict[str, object] = {
        "state_version": 1,
        "payload": b"cG9zaXRpb24tMQ",
        "witness_seq": 9,
        "predecessor_digest": b"\xaa" * 32,
    }
    values.update(overrides)
    return CursorState(**values)  # type: ignore[arg-type]


def context(**overrides: object) -> tuple[PollContext, ScopedCredentialResolver]:
    handle = CredentialHandle(reference="handle-0001")
    resolver = ScopedCredentialResolver(handle, b"tokenAAA")
    values: dict[str, object] = {
        "workspace_id": "workspace-alpha",
        "run_id": "run-0001",
        "attempt_ordinal": 1,
        "granted_scopes": ("source.scope_a",),
        "credential_handle": handle,
        "resolve_credential": resolver,
        "limits": FAKE_LIMITS,
        "deadline": Deadline(expires_at_us=NOW_US + 1_000_000),
        "cancellation": lambda: False,
    }
    values.update(overrides)
    return PollContext(**values), resolver  # type: ignore[arg-type]


# --- the SPI surface ---------------------------------------------------------------


def test_the_spi_has_exactly_the_four_accepted_operations() -> None:
    assert SPI_OPERATIONS == ("describe", "migrate_cursor", "probe", "poll")
    connector = FakeSourceConnector(script=SourceScript())
    assert isinstance(connector, SourceConnectorSpi)
    for name in SPI_OPERATIONS:
        assert callable(getattr(connector, name))
    for name in ("write", "delete", "schedule", "open_workspace", "get_credential"):
        assert not hasattr(connector, name), name


def test_the_poll_context_hands_a_connector_no_storage() -> None:
    assert poll_context_surface_defects() == ()
    fields = tuple(PollContext.__dataclass_fields__)
    assert fields == (
        "workspace_id",
        "run_id",
        "attempt_ordinal",
        "granted_scopes",
        "credential_handle",
        "resolve_credential",
        "limits",
        "deadline",
        "cancellation",
    )


def test_a_context_field_that_carried_storage_would_be_reported() -> None:
    """The surface check reads the real type, so its own failure mode is testable."""
    from omnivia_core.connector.spi import POLL_CONTEXT_FORBIDDEN_MEMBERS

    assert "connection" in POLL_CONTEXT_FORBIDDEN_MEMBERS
    assert "workspace_path" in POLL_CONTEXT_FORBIDDEN_MEMBERS
    assert "workspace_id" not in POLL_CONTEXT_FORBIDDEN_MEMBERS


# --- cursor payload domains ---------------------------------------------------------


@pytest.mark.parametrize(
    "payload, expected",
    [
        (b"cG9zaXRpb24tMQ", None),
        (b"", None),
        (b"cG9zaXRpb24.MQ", ERROR_CONNECTOR_STATE_INVALID),
        (b"cG9zaXRpb24tMQ==", ERROR_CONNECTOR_STATE_INVALID),
        (b"cG9zaXRpb2\x2bMQ", ERROR_CONNECTOR_STATE_INVALID),
        (b"cG9zaXRpb2\x2fMQ", ERROR_CONNECTOR_STATE_INVALID),
        (b"A" * 13, ERROR_CONNECTOR_STATE_INVALID),
        (b"A" * MAX_CURSOR_PAYLOAD_BYTES, None),
        (b"A" * (MAX_CURSOR_PAYLOAD_BYTES + 1), ERROR_CODE_SIZE_LIMIT_EXCEEDED),
    ],
)
def test_a_payload_is_classified_without_being_decoded(
    payload: bytes, expected: str | None
) -> None:
    assert classify_payload(payload) == expected


def test_the_encoding_check_precedes_every_other_cursor_check() -> None:
    """It is enforced by the type, so no malformed value can reach a binding,
    lineage or witness comparison at all."""
    error = _refused(
        lambda: CursorState(
            state_version=1,
            payload=b"not-base64!",
            witness_seq=1,
            predecessor_digest=b"\xff" * 32,
        )
    )
    assert error.error == ERROR_CONNECTOR_STATE_INVALID


@pytest.mark.parametrize("digest", [b"", b"\x00" * 31, b"\x00" * 33])
def test_a_predecessor_is_absent_or_exactly_thirty_two_bytes(digest: bytes) -> None:
    with pytest.raises(ConnectorContractError):
        cursor(predecessor_digest=digest)


@pytest.mark.parametrize("value", [-1, True, 2**63])
def test_unsigned_cursor_domains_are_enforced(value: object) -> None:
    with pytest.raises(ConnectorContractError):
        cursor(witness_seq=value)
    with pytest.raises(ConnectorContractError):
        cursor(state_version=value)


# --- the canonical digest -----------------------------------------------------------


def test_the_preimage_is_seven_length_prefixed_frames_in_the_fixed_order() -> None:
    state = cursor()
    preimage = cursor_digest_preimage(BINDING, state)
    fields = [
        CURSOR_DIGEST_DOMAIN_TAG,
        b"workspace-alpha",
        b"fake.recordset",
        b"1",
        b"cG9zaXRpb24tMQ",
        b"9",
        b"\xaa" * 32,
    ]
    offset = 0
    for value in fields:
        assert preimage[offset : offset + 4] == len(value).to_bytes(4, "big")
        offset += 4
        assert preimage[offset : offset + len(value)] == value
        offset += len(value)
    assert offset == len(preimage)
    assert canonical_cursor_digest(BINDING, state) == hashlib.sha256(preimage).digest()


def test_genesis_is_exactly_a_zero_length_final_frame() -> None:
    preimage = cursor_digest_preimage(BINDING, cursor(predecessor_digest=None))
    assert preimage[-4:] == b"\x00\x00\x00\x00"
    assert len(preimage) == len(cursor_digest_preimage(BINDING, cursor())) - 32


@pytest.mark.parametrize(
    "omitted",
    [
        "workspace_id",
        "connector_id",
        "state_version",
        "payload",
        "witness_seq",
        "predecessor_digest",
    ],
)
def test_omitting_any_field_from_the_preimage_produces_a_false_collision(
    omitted: str,
) -> None:
    """Each of the six is load-bearing: drop it and two distinguishable states
    hash alike, which is exactly the A6-R2 defect A6-R3 repaired."""
    left_binding, right_binding = BINDING, BINDING
    left, right = cursor(), cursor()
    if omitted == "workspace_id":
        right_binding = replace(BINDING, workspace_id="workspace-beta")
    elif omitted == "connector_id":
        right_binding = replace(BINDING, connector_id="fake.filesystem")
    elif omitted == "state_version":
        right = cursor(state_version=2)
    elif omitted == "payload":
        right = cursor(payload=b"cG9zaXRpb24tMg")
    elif omitted == "witness_seq":
        right = cursor(witness_seq=10)
    else:
        right = cursor(predecessor_digest=b"\xbb" * 32)

    assert canonical_cursor_digest(left_binding, left) != canonical_cursor_digest(
        right_binding, right
    )

    def reduced(binding: CursorBinding, state: CursorState) -> bytes:
        frames = {
            "workspace_id": binding.workspace_id.encode(),
            "connector_id": binding.connector_id.encode(),
            "state_version": str(state.state_version).encode(),
            "payload": state.payload,
            "witness_seq": str(state.witness_seq).encode(),
            "predecessor_digest": state.predecessor_digest or b"",
        }
        del frames[omitted]
        body = b"".join(
            len(value).to_bytes(4, "big") + value for value in frames.values()
        )
        return hashlib.sha256(body).digest()

    assert reduced(left_binding, left) == reduced(right_binding, right)


def test_the_digest_matches_the_corpus_golden_chain() -> None:
    """The first two links of `CON-C059`, restated as literals here so a change
    to the fixture and a change to the code cannot cancel out."""
    genesis = CursorState(
        state_version=1, payload=b"Z2VuZXNpcy0w", witness_seq=0, predecessor_digest=None
    )
    first = canonical_cursor_digest(BINDING, genesis)
    assert first.hex() == (
        "1291d543cae5c7aa08c694f434887a2e2623cc77b6f74e3a1917e08fefc03289"
    )
    child = CursorState(
        state_version=1,
        payload=b"cG9zaXRpb24tMQ",
        witness_seq=1,
        predecessor_digest=first,
    )
    assert canonical_cursor_digest(BINDING, child).hex() == (
        "ea3f3ba43cf7c1097e8c940ef4b8224496491852d5b16f9929d95614edeed3a6"
    )


# --- successor validation -------------------------------------------------------------


def _refused(call: object) -> ConnectorRefused:
    with pytest.raises(ConnectorRefused) as caught:
        call()  # type: ignore[operator]
    return caught.value


def test_a_correct_successor_is_accepted_and_returns_the_verified_digest() -> None:
    record = CursorRecord(binding=BINDING, state=cursor())
    successor = cursor(
        witness_seq=10, predecessor_digest=canonical_cursor_digest(BINDING, record.state)
    )
    assert validate_successor(
        record, successor, current_binding=BINDING
    ) == canonical_cursor_digest(BINDING, record.state)


def test_a_foreign_binding_is_refused_before_the_lineage_comparison() -> None:
    record = CursorRecord(binding=BINDING, state=cursor())
    successor = cursor(witness_seq=10, predecessor_digest=b"\x00" * 32)
    error = _refused(
        lambda: validate_successor(
            record,
            successor,
            current_binding=replace(BINDING, connector_id="fake.filesystem"),
        )
    )
    assert error.error == ERROR_CONNECTOR_CURSOR_FOREIGN


def test_a_successor_naming_another_state_is_refused_as_an_invalid_state() -> None:
    record = CursorRecord(binding=BINDING, state=cursor())
    successor = cursor(witness_seq=10, predecessor_digest=b"\x00" * 32)
    error = _refused(
        lambda: validate_successor(record, successor, current_binding=BINDING)
    )
    assert error.error == ERROR_CONNECTOR_STATE_INVALID


def test_a_regressing_witness_is_refused_and_names_both_witnesses() -> None:
    record = CursorRecord(binding=BINDING, state=cursor())
    successor = cursor(
        witness_seq=4, predecessor_digest=canonical_cursor_digest(BINDING, record.state)
    )
    error = _refused(
        lambda: validate_successor(record, successor, current_binding=BINDING)
    )
    assert error.error == ERROR_CONNECTOR_CURSOR_NOT_MONOTONIC
    assert "9" in error.detail and "4" in error.detail


def test_an_equal_witness_is_not_a_regression() -> None:
    record = CursorRecord(binding=BINDING, state=cursor())
    successor = cursor(
        witness_seq=9, predecessor_digest=canonical_cursor_digest(BINDING, record.state)
    )
    validate_successor(record, successor, current_binding=BINDING)


# --- migration --------------------------------------------------------------------------


def test_a_correct_migration_records_both_canonical_digests() -> None:
    record = CursorRecord(binding=BINDING, state=cursor())
    migrated = cursor(state_version=2, payload=b"cG9zaXRpb24tMg")
    audit = validate_migration(record, migrated, supported_state_versions=(1, 2))
    assert audit.after.witness_seq == record.state.witness_seq
    assert audit.after.predecessor_digest == record.state.predecessor_digest
    assert audit.predecessor_digest_before == canonical_cursor_digest(
        BINDING, record.state
    )
    assert audit.predecessor_digest_after == canonical_cursor_digest(BINDING, migrated)
    assert audit.predecessor_digest_before != audit.predecessor_digest_after


@pytest.mark.parametrize(
    "migrated",
    [
        pytest.param({"state_version": 1}, id="not-increasing"),
        pytest.param({"state_version": 9}, id="outside-the-supported-set"),
        pytest.param({"state_version": 2, "witness_seq": 10}, id="witness-moved"),
        pytest.param({"state_version": 2, "witness_seq": 8}, id="witness-regressed"),
        pytest.param(
            {"state_version": 2, "predecessor_digest": b"\xbb" * 32}, id="reparented"
        ),
        pytest.param(
            {"state_version": 2, "predecessor_digest": None}, id="predecessor-dropped"
        ),
    ],
)
def test_each_migration_obligation_is_enforced(migrated: dict[str, object]) -> None:
    record = CursorRecord(binding=BINDING, state=cursor())
    error = _refused(
        lambda: validate_migration(
            record, cursor(**migrated), supported_state_versions=(1, 2)
        )
    )
    assert error.error == ERROR_CONNECTOR_STATE_INVALID


def test_absent_to_present_breaks_the_same_obligation_as_present_to_absent() -> None:
    record = CursorRecord(binding=BINDING, state=cursor(predecessor_digest=None))
    error = _refused(
        lambda: validate_migration(
            record,
            cursor(state_version=2, predecessor_digest=b"\xcc" * 32),
            supported_state_versions=(1, 2),
        )
    )
    assert error.error == ERROR_CONNECTOR_STATE_INVALID


def test_an_unmigratable_state_version_declares_a_resynchronization() -> None:
    connector = FakeSourceConnector(script=SourceScript(), supported_state_versions=(1, 2))
    record = CursorRecord(binding=BINDING, state=cursor(state_version=7))
    outcome = run_cursor_migration(connector, record, supported_state_versions=(1, 2))
    assert outcome.outcome == "resync_required"
    assert outcome.error == ERROR_CONNECTOR_CURSOR_UNMIGRATABLE
    assert outcome.audit is None


def test_a_nondeterministic_migration_is_refused() -> None:
    connector = FakeSourceConnector(
        script=SourceScript(),
        supported_state_versions=(1, 2),
        migration_defect="nondeterministic",
    )
    record = CursorRecord(binding=BINDING, state=cursor())
    outcome = run_cursor_migration(connector, record, supported_state_versions=(1, 2))
    assert outcome.outcome == "refused"
    assert "deterministic" in outcome.detail


def test_migration_resolves_no_credential() -> None:
    connector = FakeSourceConnector(script=SourceScript(), supported_state_versions=(1, 2))
    _, resolver = context()
    record = CursorRecord(binding=BINDING, state=cursor())
    run_cursor_migration(connector, record, supported_state_versions=(1, 2))
    assert resolver.resolved_material == ()


# --- registration ---------------------------------------------------------------------------


def test_an_unknown_required_major_is_refused_and_an_additive_minor_is_not() -> None:
    describe = FakeSourceConnector(
        script=SourceScript(), spi_version=SpiVersion(2, 0)
    ).describe()
    error = _refused(
        lambda: validate_registration(describe, granted_capabilities=frozenset())
    )
    assert error.error == ERROR_CODE_INCOMPATIBLE_VERSION
    tolerated = FakeSourceConnector(
        script=SourceScript(), spi_version=SpiVersion(1, 7)
    ).describe()
    validate_registration(tolerated, granted_capabilities=frozenset())


def test_a_self_scheduling_declaration_is_refused() -> None:
    describe = FakeSourceConnector(
        script=SourceScript(), scheduling_declaration="every-5-minutes"
    ).describe()
    error = _refused(
        lambda: validate_registration(describe, granted_capabilities=frozenset())
    )
    assert error.error == "connector_scheduling_denied"


# --- credentials ------------------------------------------------------------------------------


def test_a_handle_carries_nothing_and_renders_redacted() -> None:
    handle = CredentialHandle(reference="handle-0001")
    assert "handle-0001" not in repr(handle)
    assert "handle-0001" not in str(handle)


def test_material_does_not_outlive_its_poll() -> None:
    ctx, resolver = context()
    assert resolver(ctx.credential_handle) == b"tokenAAA"
    resolver.invalidate()
    error = _refused(lambda: resolver(ctx.credential_handle))
    assert error.error == ERROR_CODE_AUTHORIZATION_DENIED


def test_the_comparison_catches_declared_encodings_and_nothing_else() -> None:
    material = b"tokenAAA"
    for form in declared_encodings(material):
        assert contains_known_material(b"prefix" + form + b"suffix", (material,))
    assert not contains_known_material(bytes(reversed(material)), (material,))
    assert not contains_known_material(bytes(b ^ 0x5A for b in material), (material,))


# --- hostile input --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"source_native_id": "rec\x07id"},
        {"source_native_id": "rec\x00id"},
        {"source_locator": "fake://scope-a/../../etc/shadow"},
        {"metadata_json": "[" * 40 + "]" * 40},
        {"deletion_signal": "invented"},
        {"content_checksum": "md5:0123"},
        {"permission_labels": ["Not A Label"]},
    ],
)
def test_hostile_observation_documents_are_refused_before_persistence(
    override: dict[str, object],
) -> None:
    document: dict[str, object] = {
        "source_native_id": "rec-0001",
        "source_locator": "fake://scope-a/rec-0001",
        "observed_at_us": NOW_US,
        "metadata_bytes": 128,
        "content_checksum": "sha256:" + "1" * 64,
        "media_type": "text/plain",
        "permission_labels": ["workspace.member"],
        "deletion_signal": "none",
    }
    document.update(override)
    error = _refused(lambda: admit_observation(document))
    assert error.error == ERROR_CODE_INVALID_REQUEST
    for value in override.values():
        assert str(value) not in str(error)


def test_the_depth_scan_ignores_brackets_inside_string_literals() -> None:
    assert metadata_depth('{"a": {"b": "[[[[["}}') == 2
    assert metadata_depth('{"a": "\\""}') == 1
    assert metadata_depth("[" * 40 + "]" * 40) == 40


def test_an_unknown_optional_field_is_tolerated() -> None:
    observation = admit_observation(
        {
            "source_native_id": "rec-0001",
            "source_locator": "fake://scope-a/rec-0001",
            "observed_at_us": NOW_US,
            "metadata_bytes": 128,
            "deletion_signal": "none",
            "an_additive_minor_field": 1,
        }
    )
    assert observation.source_native_id == "rec-0001"


# --- batch admission ----------------------------------------------------------------------------------


def batch_for(record: CursorRecord, *observations: Observation, witness: int = 10) -> Batch:
    return Batch(
        observations=observations,
        successor_cursor=CursorState(
            state_version=1,
            payload=b"cG9zaXRpb24tMg",
            witness_seq=witness,
            predecessor_digest=canonical_cursor_digest(record.binding, record.state),
        ),
    )


def descriptor(**overrides: object) -> object:
    return FakeSourceConnector(script=SourceScript(), **overrides).describe()  # type: ignore[arg-type]


def test_the_host_counts_the_batch_itself() -> None:
    ctx, resolver = context()
    record = CursorRecord(binding=BINDING, state=cursor())
    batch = batch_for(record, *synthetic_observations("rec", 101))
    error = _refused(
        lambda: validate_batch(
            batch,
            ctx,
            record,
            descriptor=descriptor(),  # type: ignore[arg-type]
            now_us=NOW_US,
            resolved_material=resolver.resolved_material,
        )
    )
    assert error.error == ERROR_CODE_SIZE_LIMIT_EXCEEDED
    assert "101" in error.detail


def test_an_expired_deadline_refuses_before_anything_else() -> None:
    ctx, resolver = context(deadline=Deadline(expires_at_us=NOW_US - 1))
    record = CursorRecord(binding=BINDING, state=cursor())
    batch = batch_for(record, *synthetic_observations("rec", 101))
    error = _refused(
        lambda: validate_batch(
            batch,
            ctx,
            record,
            descriptor=descriptor(),  # type: ignore[arg-type]
            now_us=NOW_US,
            resolved_material=resolver.resolved_material,
        )
    )
    assert error.error == ERROR_CODE_DEADLINE_EXCEEDED


def test_an_oversized_item_is_dead_lettered_and_the_rest_commits() -> None:
    ctx, resolver = context()
    record = CursorRecord(binding=BINDING, state=cursor())
    normal, big = synthetic_observations("rec", 2)
    verdict = validate_batch(
        batch_for(record, normal, replace(big, metadata_bytes=131072)),
        ctx,
        record,
        descriptor=descriptor(),  # type: ignore[arg-type]
        now_us=NOW_US,
        resolved_material=resolver.resolved_material,
    )
    assert verdict.outcome == "accepted"
    assert [item.source_native_id for item in verdict.observations] == ["rec-0000"]
    assert [item.source_native_id for item in verdict.item_failures] == ["rec-0001"]


def test_the_run_byte_ceiling_is_measured_against_admitted_items() -> None:
    ctx, resolver = context(
        limits=replace(FAKE_LIMITS, max_run_bytes=200)
    )
    record = CursorRecord(binding=BINDING, state=cursor())
    batch = batch_for(record, *synthetic_observations("rec", 2))
    error = _refused(
        lambda: validate_batch(
            batch,
            ctx,
            record,
            descriptor=descriptor(),  # type: ignore[arg-type]
            now_us=NOW_US,
            resolved_material=resolver.resolved_material,
        )
    )
    assert error.error == ERROR_CODE_SIZE_LIMIT_EXCEEDED


def test_a_cursor_payload_carrying_resolved_material_is_refused() -> None:
    ctx, resolver = context()
    resolver(ctx.credential_handle)
    record = CursorRecord(binding=BINDING, state=cursor())
    leaking = Batch(
        observations=(),
        successor_cursor=CursorState(
            state_version=1,
            payload=b"cG9zaXRpb24tMg" + b"tokenAAA",
            witness_seq=10,
            predecessor_digest=canonical_cursor_digest(BINDING, record.state),
        ),
    )
    error = _refused(
        lambda: validate_batch(
            leaking,
            ctx,
            record,
            descriptor=descriptor(),  # type: ignore[arg-type]
            now_us=NOW_US,
            resolved_material=resolver.resolved_material,
        )
    )
    assert error.error == ERROR_CONNECTOR_SECRET_EXPOSED


def test_an_empty_batch_that_stands_still_is_no_change_and_one_that_moves_is_not() -> None:
    ctx, resolver = context()
    record = CursorRecord(binding=BINDING, state=cursor())
    still = validate_batch(
        batch_for(record, witness=9),
        ctx,
        record,
        descriptor=descriptor(),  # type: ignore[arg-type]
        now_us=NOW_US,
        resolved_material=resolver.resolved_material,
    )
    assert still.outcome == "no_change"
    moved = validate_batch(
        batch_for(record, witness=100),
        ctx,
        record,
        descriptor=descriptor(),  # type: ignore[arg-type]
        now_us=NOW_US,
        resolved_material=resolver.resolved_material,
    )
    assert moved.outcome == "accepted"
    assert moved.successor.state.witness_seq == 100


def test_a_locator_derived_connector_defers_rather_than_guesses() -> None:
    ctx, resolver = context()
    record = CursorRecord(binding=BINDING, state=cursor())
    verdict = validate_batch(
        batch_for(record, *synthetic_observations("rec", 1)),
        ctx,
        record,
        descriptor=descriptor(  # type: ignore[arg-type]
            identity_stability=IdentityStability.LOCATOR_DERIVED
        ),
        now_us=NOW_US,
        resolved_material=resolver.resolved_material,
    )
    assert verdict.outcome == "deferred_to_reconciliation"


# --- the fake -------------------------------------------------------------------------------------------


def test_the_fake_replays_byte_identically_and_resumes_from_the_cursor_alone() -> None:
    script = SourceScript(
        windows=(
            SourceWindow(witness_seq=1, observations=synthetic_observations("rec", 2)),
            SourceWindow(
                witness_seq=2, observations=synthetic_observations("rec", 2, start=2)
            ),
        )
    )
    ctx, resolver = context()
    genesis = CursorState(state_version=1, payload=b"d2luZG93LTAwMDA", witness_seq=0)
    first = list(FakeSourceConnector(script=script).poll(ctx, genesis))
    second = list(FakeSourceConnector(script=script).poll(ctx, genesis))
    assert first == second
    assert resolver.resolved_material == ()

    resumed = list(FakeSourceConnector(script=script).poll(ctx, first[0].successor_cursor))
    assert len(resumed) == 1
    assert resumed[0].observations == script.windows[1].observations


def test_the_fake_stops_at_a_batch_boundary_when_cancelled() -> None:
    script = SourceScript(
        windows=(
            SourceWindow(witness_seq=1, observations=synthetic_observations("rec", 2)),
            SourceWindow(
                witness_seq=2, observations=synthetic_observations("rec", 2, start=2)
            ),
        )
    )
    seen = [0]

    def cancellation() -> bool:
        seen[0] += 1
        return seen[0] > 1

    ctx, _ = context(cancellation=cancellation)
    genesis = CursorState(state_version=1, payload=b"d2luZG93LTAwMDA", witness_seq=0)
    assert len(list(FakeSourceConnector(script=script).poll(ctx, genesis))) == 1

    ctx_all, _ = context(cancellation=lambda: True)
    assert list(FakeSourceConnector(script=script).poll(ctx_all, genesis)) == []


def test_the_fake_chains_every_successor_onto_the_state_it_followed() -> None:
    script = SourceScript(
        windows=(
            SourceWindow(witness_seq=1, observations=synthetic_observations("rec", 1)),
            SourceWindow(
                witness_seq=2, observations=synthetic_observations("rec", 1, start=1)
            ),
        )
    )
    ctx, _ = context()
    genesis = CursorState(state_version=1, payload=b"d2luZG93LTAwMDA", witness_seq=0)
    record = CursorRecord(binding=BINDING, state=genesis)
    for batch in FakeSourceConnector(script=script).poll(ctx, genesis):
        validate_successor(record, batch.successor_cursor, current_binding=BINDING)
        record = CursorRecord(binding=BINDING, state=batch.successor_cursor)
