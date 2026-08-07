"""Semantic validation for public service endpoint descriptors."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from omnivia_core.contracts import v1
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    ContractDecodeError,
    ServiceEndpointDescriptor,
    ServiceProbeResult,
    ServiceProcessEvidence,
    VersionWindow,
)
from omnivia_core.contracts.v1.semantics_service import (
    decode_service_endpoint_descriptor,
    decode_service_probe_result,
    encode_service_endpoint_descriptor,
    encode_service_probe_result,
    validate_service_endpoint_descriptor,
    validate_service_endpoint_uri,
    validate_service_probe_result,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_PATH = (
    REPO_ROOT
    / "tests"
    / "contracts"
    / "fixtures"
    / "service-endpoint-uri-policy-v1.json"
)
PUBLISHED_AT_FIXTURE_PATH = (
    REPO_ROOT
    / "tests"
    / "contracts"
    / "fixtures"
    / "descriptor-published-at-policy-v1.json"
)
TIMESTAMP_SCHEMA_PATH = (
    REPO_ROOT / "contracts" / "application" / "v1" / "schemas" / "common.schema.json"
)

#: An endpoint the URI policy accepts, so a case here can only fail on its timestamp.
POLICY_CLEAN_ENDPOINT = "unix:///run/omnivia/core.sock"

#: The stable refusal a caller may match on. Written out rather than imported so
#: that rewording it is a visible contract change here, not a silent one.
PUBLISHED_AT_REFUSAL = "published_at is not a canonical RFC 3339 UTC Timestamp"


def _fixture() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return document


def _descriptor_document(endpoint_uri: str) -> dict[str, Any]:
    return {
        "descriptor_version": "1.0",
        "workspace_id": "workspace-1",
        "service_instance_id": "instance-9c21",
        "installation_id": "installation-4b70",
        "endpoint_uri": endpoint_uri,
        "protocol_version": "1.2",
        "server_version": "1.2.5",
        "supported_api_versions": {"minimum": "1.0", "maximum": "1.2"},
        "supported_workspace_versions": {"minimum": "1.0", "maximum": "1.1"},
        "workspace_format_version": "1.1",
        "ready": True,
        "lifecycle_state": "serving",
        "fencing_generation": 7,
        "published_at": "2026-07-30T00:00:00Z",
    }


def _descriptor_value(endpoint_uri: str) -> ServiceEndpointDescriptor:
    return ServiceEndpointDescriptor(
        descriptor_version="1.0",
        workspace_id="workspace-1",
        service_instance_id="instance-9c21",
        installation_id="installation-4b70",
        endpoint_uri=endpoint_uri,
        protocol_version="1.2",
        server_version="1.2.5",
        supported_api_versions=VersionWindow(minimum="1.0", maximum="1.2"),
        supported_workspace_versions=VersionWindow(minimum="1.0", maximum="1.1"),
        workspace_format_version="1.1",
        ready=True,
        lifecycle_state="serving",
        fencing_generation=7,
        published_at="2026-07-30T00:00:00Z",
    )


def _probe_result_document(endpoint_uri: str) -> dict[str, Any]:
    return {
        "probe": "service.discover",
        "status": "pass",
        "server_version": "1.2.5",
        "api_version": "1.2",
        "observed_at": "2026-07-30T00:00:00Z",
        "descriptor": _descriptor_document(endpoint_uri),
    }


def test_service_endpoint_semantics_are_on_the_public_v1_surface() -> None:
    assert v1.decode_service_endpoint_descriptor is decode_service_endpoint_descriptor
    assert v1.decode_service_probe_result is decode_service_probe_result
    assert v1.encode_service_endpoint_descriptor is encode_service_endpoint_descriptor
    assert v1.encode_service_probe_result is encode_service_probe_result
    assert v1.validate_service_endpoint_descriptor is validate_service_endpoint_descriptor
    assert v1.validate_service_endpoint_uri is validate_service_endpoint_uri
    assert v1.validate_service_probe_result is validate_service_probe_result


@pytest.mark.parametrize(
    "case", [*_fixture()["accepted"], *_fixture()["rejected"]], ids=lambda c: c["id"]
)
def test_the_narrow_endpoint_validator_returns_the_descriptor_validator_verdict(
    case: dict[str, str],
) -> None:
    """One policy, asked two ways, over the whole endpoint corpus.

    `validate_service_endpoint_uri` exists so a caller holding one field -- Runtime's
    probe boundary -- can ask about that field instead of catching a whole-descriptor
    refusal it cannot attribute. It is worth having only if it is the same policy: a
    second, narrower rule that drifted from the descriptor's would be the exact defect
    the shared endpoint fixture exists to catch, one layer further in.
    """
    endpoint_uri = case["endpoint_uri"]
    document = _descriptor_document(endpoint_uri)

    def descriptor_verdict() -> bool:
        try:
            decode_service_endpoint_descriptor(document)
        except ContractSemanticError:
            return False
        return True

    def narrow_verdict() -> bool:
        try:
            validate_service_endpoint_uri(endpoint_uri)
        except ContractSemanticError:
            return False
        return True

    assert narrow_verdict() == descriptor_verdict()
    assert narrow_verdict() is (case in _fixture()["accepted"])


def test_the_narrow_endpoint_validator_ignores_every_other_descriptor_field() -> None:
    """The whole point of the narrowing: it answers about the URI and nothing else.

    A descriptor the full validator refuses for its `published_at` must leave this
    one silent, or Runtime's endpoint label would go on mis-attributing exactly the
    way it did before.
    """
    document = _document_with_published_at("not-a-timestamp")
    with pytest.raises(ContractSemanticError):
        decode_service_endpoint_descriptor(document)
    validate_service_endpoint_uri(document["endpoint_uri"])


@pytest.mark.parametrize("case", _fixture()["accepted"], ids=lambda case: case["id"])
def test_public_semantics_accept_approved_dialable_endpoint(case: dict[str, str]) -> None:
    endpoint_uri = case["endpoint_uri"]
    descriptor = decode_service_endpoint_descriptor(_descriptor_document(endpoint_uri))
    validate_service_endpoint_descriptor(descriptor)
    assert encode_service_endpoint_descriptor(descriptor)["endpoint_uri"] == endpoint_uri


@pytest.mark.parametrize("case", _fixture()["rejected"], ids=lambda case: case["id"])
def test_public_semantics_reject_unsafe_endpoint_without_echoing_it(
    case: dict[str, str],
) -> None:
    endpoint_uri = case["endpoint_uri"]
    with pytest.raises(ContractSemanticError) as decoded_error:
        decode_service_endpoint_descriptor(_descriptor_document(endpoint_uri))
    assert endpoint_uri not in str(decoded_error.value)

    descriptor = _descriptor_value(endpoint_uri)
    with pytest.raises(ContractSemanticError) as validation_error:
        validate_service_endpoint_descriptor(descriptor)
    assert endpoint_uri not in str(validation_error.value)

    with pytest.raises(ContractSemanticError) as encoded_error:
        encode_service_endpoint_descriptor(descriptor)
    assert endpoint_uri not in str(encoded_error.value)


@pytest.mark.parametrize("case", _fixture()["rejected"], ids=lambda case: case["id"])
def test_probe_result_semantics_reject_nested_unsafe_descriptor_without_echoing_it(
    case: dict[str, str],
) -> None:
    endpoint_uri = case["endpoint_uri"]
    document = _probe_result_document(endpoint_uri)
    with pytest.raises(ContractSemanticError) as decoded_error:
        decode_service_probe_result(document)
    assert endpoint_uri not in str(decoded_error.value)

    result = ServiceProbeResult.from_wire(document)
    with pytest.raises(ContractSemanticError) as validation_error:
        validate_service_probe_result(result)
    assert endpoint_uri not in str(validation_error.value)

    with pytest.raises(ContractSemanticError) as encoded_error:
        encode_service_probe_result(result)
    assert endpoint_uri not in str(encoded_error.value)


def test_process_evidence_spelling_is_not_constrained_by_endpoint_semantics() -> None:
    """P0 governs the endpoint URI only; process start-time spelling stays opaque.

    `start_time` is compared for equality against a later reading of the same pid
    and never parsed, so this layer must not start refusing whatever spelling a
    host platform happens to report.
    """
    descriptor = replace(
        _descriptor_value("unix:///run/omnivia/core.sock"),
        process=ServiceProcessEvidence(
            pid=4242,
            start_time="Mon Jul 27 09:14:02 2026",
            boot_id="boot-1",
        ),
    )
    validate_service_endpoint_descriptor(descriptor)
    assert encode_service_endpoint_descriptor(descriptor)["process"] == {
        "pid": 4242,
        "start_time": "Mon Jul 27 09:14:02 2026",
        "boot_id": "boot-1",
    }


# --------------------------------------------------------------------------
# `published_at` is a `Timestamp`, and the public decode boundary says so
#
# `Timestamp` is a `TypeAlias = str` and the generated decoder reads the field
# with a bare string decoder, so before this the boundary that exists to enforce
# the contract accepted `published_at="not-a-timestamp"` and a full discovery
# succeeded on it. Nothing downstream reads the value, which is exactly why the
# defect survived: the refusal is owed to the published type, not to a consumer.
#
# The grammar is not restated here. Both halves come from the canonical schema --
# `Timestamp`'s `pattern`, reached through the generated constant, and its
# `format: date-time`, which the pattern cannot express -- and
# `test_the_canonical_schema_and_the_public_boundary_agree` is what holds the two
# surfaces to one answer rather than to two regexes that happen to look alike.
# --------------------------------------------------------------------------


def _published_at_fixture() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        PUBLISHED_AT_FIXTURE_PATH.read_text(encoding="utf-8")
    )
    return document


PUBLISHED_AT_CASES = _published_at_fixture()
SEMANTIC_REJECTS = [
    case for case in PUBLISHED_AT_CASES["invalid"] if case["error"] == "semantic"
]
DECODE_REJECTS = [
    case for case in PUBLISHED_AT_CASES["invalid"] if case["error"] == "decode"
]
CONFORMING_CASES = [
    *PUBLISHED_AT_CASES["valid"],
    *PUBLISHED_AT_CASES["compatibility"],
]


def _document_with_published_at(published_at: object) -> dict[str, Any]:
    document = _descriptor_document(POLICY_CLEAN_ENDPOINT)
    document["published_at"] = published_at
    return document


def _value_with_published_at(published_at: Any) -> ServiceEndpointDescriptor:
    return replace(
        _descriptor_value(POLICY_CLEAN_ENDPOINT), published_at=published_at
    )


def test_published_at_policy_fixture_is_deterministic_and_has_unique_cases() -> None:
    document = _published_at_fixture()
    assert document["format"] == "omnivia.descriptor-published-at-policy.v1"
    assert PUBLISHED_AT_FIXTURE_PATH.read_text(encoding="utf-8") == (
        json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    )
    ids = [
        case["id"]
        for group in ("valid", "invalid", "missing", "compatibility")
        for case in document[group]
    ]
    assert len(ids) == len(set(ids))
    assert {case["error"] for case in document["invalid"]} == {"semantic", "decode"}
    assert SEMANTIC_REJECTS and DECODE_REJECTS and CONFORMING_CASES


@pytest.mark.parametrize("case", CONFORMING_CASES, ids=lambda case: case["id"])
def test_a_canonical_published_at_still_decodes_and_round_trips(
    case: dict[str, Any],
) -> None:
    """Valid and compatibility cases: tightening must not narrow the language.

    The compatibility group carries the exact literals already published by the
    client, runtime and contract suites, so a grammar that drifted tighter than
    the schema would fail here rather than in whichever suite happened to run
    next.
    """
    published_at = case["published_at"]
    descriptor = decode_service_endpoint_descriptor(
        _document_with_published_at(published_at)
    )
    assert descriptor.published_at == published_at
    validate_service_endpoint_descriptor(descriptor)
    assert encode_service_endpoint_descriptor(descriptor)["published_at"] == published_at


@pytest.mark.parametrize("case", SEMANTIC_REJECTS, ids=lambda case: case["id"])
def test_a_non_timestamp_published_at_is_refused_without_echoing_it(
    case: dict[str, Any],
) -> None:
    """The defect itself, plus every neighbouring spelling the contract forbids.

    Refused on all three public paths, because a value that never reached the
    wire -- a hand-built dataclass handed straight to `encode` -- contradicts the
    published type just as loudly as one that did.

    The message is pinned by equality rather than by `in`. That is what makes it
    a stable typed error a caller may match on, and it proves the rejected value
    is never echoed for every case at once -- including the empty string, which
    an `x not in message` check can never fail.
    """
    published_at = case["published_at"]
    descriptor = _value_with_published_at(published_at)

    for raises in (
        lambda: decode_service_endpoint_descriptor(
            _document_with_published_at(published_at)
        ),
        lambda: validate_service_endpoint_descriptor(descriptor),
        lambda: encode_service_endpoint_descriptor(descriptor),
    ):
        with pytest.raises(ContractSemanticError) as caught:
            raises()
        assert str(caught.value) == PUBLISHED_AT_REFUSAL


@pytest.mark.parametrize("case", SEMANTIC_REJECTS, ids=lambda case: case["id"])
def test_probe_result_semantics_refuse_a_nested_non_timestamp_published_at(
    case: dict[str, Any],
) -> None:
    document = _probe_result_document(POLICY_CLEAN_ENDPOINT)
    document["descriptor"]["published_at"] = case["published_at"]
    with pytest.raises(ContractSemanticError):
        decode_service_probe_result(document)
    with pytest.raises(ContractSemanticError):
        encode_service_probe_result(ServiceProbeResult.from_wire(document))


@pytest.mark.parametrize("case", DECODE_REJECTS, ids=lambda case: case["id"])
def test_a_wrongly_typed_published_at_stays_a_structural_decode_error(
    case: dict[str, Any],
) -> None:
    """The two typed errors keep their established division of labour.

    A non-string is a structural fault and `ContractDecodeError` still names it,
    so tightening the value domain did not quietly reclassify what the decoder
    was already refusing. `ContractSemanticError` is not a subclass of it, so
    this would fail if the new check had displaced the structural one.
    """
    with pytest.raises(ContractDecodeError) as caught:
        decode_service_endpoint_descriptor(
            _document_with_published_at(case["published_at"])
        )
    assert not isinstance(caught.value, ContractSemanticError)

    # A hand-built dataclass skips the decoder, so the same value is refused by
    # the semantic layer instead of escaping to a caller unchecked.
    with pytest.raises(ContractSemanticError):
        validate_service_endpoint_descriptor(
            _value_with_published_at(case["published_at"])
        )


@pytest.mark.parametrize(
    "case", PUBLISHED_AT_CASES["missing"], ids=lambda case: case["id"]
)
def test_a_missing_published_at_stays_a_structural_decode_error(
    case: dict[str, Any],
) -> None:
    document = _descriptor_document(POLICY_CLEAN_ENDPOINT)
    del document["published_at"]
    with pytest.raises(ContractDecodeError) as caught:
        decode_service_endpoint_descriptor(document)
    assert "published_at" in str(caught.value)
    assert not isinstance(caught.value, ContractSemanticError)


def test_the_canonical_schema_and_the_public_boundary_agree() -> None:
    """The grammar is the schema's, not this module's.

    Validated against `Timestamp` itself -- `pattern` and `format: date-time`
    together, with the format checker on -- so a semantic layer that invented its
    own calendar rule, or dropped one half of the declared domain, disagrees here
    on a named case rather than passing because both sides were edited to match.
    """
    timestamp = json.loads(TIMESTAMP_SCHEMA_PATH.read_text(encoding="utf-8"))["$defs"][
        "Timestamp"
    ]
    validator = Draft202012Validator(
        timestamp, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    disagreements = [
        (case["id"], case["published_at"])
        for case in (*CONFORMING_CASES, *PUBLISHED_AT_CASES["invalid"])
        if validator.is_valid(case["published_at"])
        is not _boundary_accepts(case["published_at"])
    ]
    assert not disagreements, disagreements


def _boundary_accepts(published_at: object) -> bool:
    """Whether the public decode boundary admits this `published_at`."""
    try:
        decode_service_endpoint_descriptor(_document_with_published_at(published_at))
    except (ContractDecodeError, ContractSemanticError):
        return False
    return True


# --------------------------------------------------------------------------
# The same descriptor, the same verdict, in both generated bindings
#
# The two languages had drifted apart once already and nothing said so: the
# `published_at` check was added to Python alone, so for a year and a leap day
# the generated TypeScript published descriptors this module refuses. Neither
# existing parity gate could see it. `test_scalar_anchor_parity` compares the
# *patterns* under both regex engines, and `test_service_endpoint_policy`
# executes the compiled module against the *endpoint URI* corpus -- so both
# stayed green while the descriptor validators checked different field sets.
#
# What is compared here is therefore the verdict on a whole descriptor, over
# every string field crossed with every pinned probe value. That makes the gate
# field-agnostic: enforcing any of the eight remaining patterned fields in one
# binding and not the other fails here on a named field, without this test
# needing to know which fields are enforced today.
# --------------------------------------------------------------------------

TYPESCRIPT_PATH = (
    REPO_ROOT / "generated" / "typescript" / "application" / "v1" / "index.ts"
)


def _descriptor_probe_corpus() -> list[tuple[str, dict[str, Any]]]:
    """Every string descriptor field, crossed with every pinned probe value.

    Probes come from both policy fixtures rather than from invented strings, so a
    case that separates the two bindings is one the contract already names.
    """
    endpoint_cases = _fixture()
    probes = [
        *(case["endpoint_uri"] for case in endpoint_cases["accepted"]),
        *(case["endpoint_uri"] for case in endpoint_cases["rejected"]),
        *(
            case["published_at"]
            for group in ("valid", "invalid", "compatibility")
            for case in PUBLISHED_AT_CASES[group]
            if isinstance(case["published_at"], str)
        ),
    ]
    baseline = _descriptor_document(POLICY_CLEAN_ENDPOINT)
    corpus = [("baseline", baseline)]
    corpus += [
        (f"{field}={probe!r}", {**baseline, field: probe})
        for field, value in baseline.items()
        if isinstance(value, str)
        for probe in probes
    ]
    return corpus


def _descriptor_verdict(document: dict[str, Any]) -> bool:
    """Python's verdict on a structurally decodable descriptor document."""
    descriptor = ServiceEndpointDescriptor.from_wire(document)
    try:
        validate_service_endpoint_descriptor(descriptor)
    except ContractSemanticError:
        return False
    return True


def test_the_generated_typescript_checks_the_same_fields_this_module_checks(
    tmp_path: Path,
) -> None:
    """The generated bindings agree on every descriptor in the probe corpus.

    Compiled with the pinned `tsc` and executed, not pattern-matched out of the
    emitted source: a guard that reads correctly and computes something else is
    exactly the failure a source-level assertion cannot see.
    """
    tsc = REPO_ROOT / "node_modules" / ".bin" / "tsc"
    node = shutil.which("node")
    assert tsc.is_file(), "run npm ci so the pinned TypeScript compiler is available"
    assert node is not None, (
        "node is required for the executable TypeScript parity gate"
    )

    subprocess.run(
        [
            str(tsc),
            "--strict",
            "--skipLibCheck",
            "--target",
            "ES2022",
            "--module",
            "commonjs",
            "--outDir",
            str(tmp_path),
            str(TYPESCRIPT_PATH),
        ],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    corpus = _descriptor_probe_corpus()
    script = """
const contracts = require(process.argv[1]);
const corpus = JSON.parse(require('fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(
  corpus.map((document) =>
    contracts.isServiceEndpointDescriptorSemanticallyValid(document)),
));
"""
    finished = subprocess.run(
        [node, "-e", script, str(tmp_path / "index.js")],
        input=json.dumps([document for _, document in corpus]),
        capture_output=True,
        text=True,
        check=True,
    )
    typescript = json.loads(finished.stdout)

    python = [_descriptor_verdict(document) for _, document in corpus]
    # Both outcomes must be reachable, or an all-refusing guard would pass here.
    assert any(python) and not all(python)
    disagreements = [
        label
        for (label, _), accepted, admitted in zip(
            corpus, typescript, python, strict=True
        )
        if accepted is not admitted
    ]
    assert not disagreements, disagreements


# --------------------------------------------------------------------------
# `ServiceProbeResult.observed_at`
#
# No second fixture. `observed_at` and `published_at` are the same declared value
# domain -- both are `Timestamp` -- so the corpus that pins one pins the other, and
# a second copy of it is a second thing to keep in step. Section 5.3's standard is
# "one fixture, four surfaces", and the reason the `compatibility` group is worth
# having at all is that tightening the policy then fails in *one* obvious place;
# splitting the same domain across two files is what that group exists to prevent.
# What is genuinely different about `observed_at` is not its corpus but its
# attribution -- which field a refusal names -- and that is asserted below.
# --------------------------------------------------------------------------

#: The stable refusal a caller may match on. Written out rather than imported, for
#: the same reason `PUBLISHED_AT_REFUSAL` is.
OBSERVED_AT_REFUSAL = "observed_at is not a canonical RFC 3339 UTC Timestamp"


def _document_with_observed_at(observed_at: object) -> dict[str, Any]:
    document = _probe_result_document(POLICY_CLEAN_ENDPOINT)
    document["observed_at"] = observed_at
    return document


@pytest.mark.parametrize("case", CONFORMING_CASES, ids=lambda case: case["id"])
def test_a_conforming_observed_at_is_accepted(case: dict[str, Any]) -> None:
    """Every instant the fixture pins as publishable is publishable here too.

    The `compatibility` group is included: those are literals other suites already
    publish, and a tightening that refuses one of them is a defect in the guard
    rather than a correction to the suite.
    """
    document = _document_with_observed_at(case["published_at"])
    assert decode_service_probe_result(document).observed_at == case["published_at"]


@pytest.mark.parametrize("case", SEMANTIC_REJECTS, ids=lambda case: case["id"])
def test_a_non_timestamp_observed_at_is_refused(case: dict[str, Any]) -> None:
    """Decoded structurally, then refused semantically, under its own message."""
    document = _document_with_observed_at(case["published_at"])
    with pytest.raises(ContractSemanticError) as caught:
        decode_service_probe_result(document)
    assert str(caught.value) == OBSERVED_AT_REFUSAL
    with pytest.raises(ContractSemanticError) as raised:
        encode_service_probe_result(ServiceProbeResult.from_wire(document))
    assert str(raised.value) == OBSERVED_AT_REFUSAL


@pytest.mark.parametrize("case", DECODE_REJECTS, ids=lambda case: case["id"])
def test_a_wrongly_typed_observed_at_stays_a_structural_decode_error(
    case: dict[str, Any],
) -> None:
    """A non-string never reaches the semantic boundary, exactly as for `published_at`."""
    document = _document_with_observed_at(case["published_at"])
    with pytest.raises(ContractDecodeError):
        decode_service_probe_result(document)


def test_an_observed_at_refusal_names_its_own_field_and_not_the_descriptor() -> None:
    """The attribution, which is the whole reason this is not the descriptor's message.

    A caller that catches `ContractSemanticError` cannot otherwise tell which field
    produced it. Reporting a probe-result fault as a descriptor fault is the same
    misattribution that once published a malformed `published_at` to an
    unauthenticated caller as an endpoint-URI fault.
    """
    document = _document_with_observed_at("2026-13-01T00:00:00Z")
    with pytest.raises(ContractSemanticError) as caught:
        decode_service_probe_result(document)
    message = str(caught.value)
    assert "observed_at" in message
    assert "published_at" not in message
    assert "descriptor" not in message
    assert "endpoint_uri" not in message

    # And the converse, so the two are not merely both refused under one label.
    nested = _probe_result_document(POLICY_CLEAN_ENDPOINT)
    nested["descriptor"]["published_at"] = "2026-13-01T00:00:00Z"
    with pytest.raises(ContractSemanticError) as raised:
        decode_service_probe_result(nested)
    assert str(raised.value) == PUBLISHED_AT_REFUSAL


def test_an_observed_at_refusal_never_echoes_the_rejected_value() -> None:
    """A probe is answered before authentication, so the refusal is a fixed sentence."""
    secret = "2026-99-99T00:00:00Z-tenant-acme-shard-7"
    document = _document_with_observed_at(secret)
    with pytest.raises(ContractSemanticError) as caught:
        decode_service_probe_result(document)
    assert secret not in str(caught.value)
    assert str(caught.value) == OBSERVED_AT_REFUSAL


def test_the_generated_typescript_agrees_on_every_observed_at_case(
    tmp_path: Path,
) -> None:
    """Surface four, for `observed_at`: compiled and executed, not string-compared.

    `assertServiceProbeResultSemantics` is the TypeScript counterpart of
    `validate_service_probe_result`, and this compares the two over every pinned
    timestamp. It is the assertion that would have caught the `published_at`
    divergence had it existed for that field, applied to the sibling field one
    field away that was unenforced in both bindings.
    """
    tsc = REPO_ROOT / "node_modules" / ".bin" / "tsc"
    node = shutil.which("node")
    assert tsc.is_file(), "run npm ci so the pinned TypeScript compiler is available"
    assert node is not None, (
        "node is required for the executable TypeScript parity gate"
    )

    subprocess.run(
        [
            str(tsc),
            "--strict",
            "--skipLibCheck",
            "--target",
            "ES2022",
            "--module",
            "commonjs",
            "--outDir",
            str(tmp_path),
            str(TYPESCRIPT_PATH),
        ],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    cases = [
        (case["id"], case["published_at"])
        for group in ("valid", "invalid", "compatibility")
        for case in PUBLISHED_AT_CASES[group]
        if isinstance(case["published_at"], str)
    ]
    documents = [_document_with_observed_at(value) for _, value in cases]

    script = """
const contracts = require(process.argv[1]);
const corpus = JSON.parse(require('fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(corpus.map((document) => {
  try {
    contracts.assertServiceProbeResultSemantics(document);
    return true;
  } catch (error) {
    return false;
  }
})));
"""
    finished = subprocess.run(
        [node, "-e", script, str(tmp_path / "index.js")],
        input=json.dumps(documents),
        capture_output=True,
        text=True,
        check=True,
    )
    typescript = json.loads(finished.stdout)

    def python_verdict(document: dict[str, Any]) -> bool:
        try:
            validate_service_probe_result(ServiceProbeResult.from_wire(document))
        except ContractSemanticError:
            return False
        return True

    python = [python_verdict(document) for document in documents]
    # Both outcomes must be reachable, or an all-refusing guard would pass here.
    assert any(python) and not all(python)
    disagreements = [
        case_id
        for (case_id, _), accepted, admitted in zip(cases, typescript, python, strict=True)
        if accepted is not admitted
    ]
    assert not disagreements, disagreements
