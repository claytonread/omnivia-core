"""The provider-SPI conformance corpus: loading it, and the shape of a report.

Standard library only, and execution-free. What this module does is read
`docs/quality/fixtures/core-agent-host-provider-spi/provider-spi-cases.json`
into value objects a later lane can execute against a provider, and refuse to
return anything at all when the bytes on disk are not the accepted corpus.

Two things it deliberately does not do. It does not drive a provider, map a
case's `given`/`when` onto an SPI request, or decide whether a case passed --
that is the executing lane's work, and this module only fixes the vocabulary it
will report in. And no report DTO here derives an outcome from a case
identifier: a report that inferred its own answer from the corpus would be
green against an empty implementation.

Everything handed back is immutable, `given` and `when` included: their nested
lists become tuples and their nested objects mapping proxies, so a caller
holding a loaded case cannot edit the corpus out from under the next reader of
it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeVar, cast

from omnivia_core.agent_host.spi import SPI_VERSION, Disposition, Hook

#: The corpus this loader accepts, exactly.
EXPECTED_FIXTURE_ID: Final = "core-agent-host-provider-spi"
EXPECTED_FIXTURE_VERSION: Final = "1.1.0"
EXPECTED_CASE_COUNT: Final = 42

#: The identifiers the accepted corpus fixes, in order.
EXPECTED_CASE_IDS: Final[tuple[str, ...]] = tuple(
    f"SPI-V-{index:03d}" for index in range(1, EXPECTED_CASE_COUNT + 1)
)

#: The disposition totals the accepted corpus carries. A corpus whose totals
#: had drifted is a different corpus, whatever its version string says.
EXPECTED_DISPOSITION_TOTALS: Final[Mapping[Disposition, int]] = MappingProxyType(
    {
        Disposition.REFUSED: 28,
        Disposition.IDEMPOTENT_REPLAY: 5,
        Disposition.ACCEPTED: 3,
        Disposition.IGNORED: 2,
        Disposition.ESCALATION_REQUIRED: 2,
        Disposition.NEGOTIATED: 2,
    }
)

#: The ten hooks in the order the accepted corpus lists them -- which is the
#: corpus's own order, not the declaration order of :class:`Hook`. Exactly these
#: ten, once each: a corpus that repeated, dropped or reordered one is stating a
#: different hook vocabulary from the one the cases were written against.
EXPECTED_HOOKS: Final[tuple[Hook, ...]] = (
    Hook.NEGOTIATE,
    Hook.RECALL_BEFORE_TURN,
    Hook.CAPTURE_AFTER_TURN,
    Hook.TOOL_RESULT_PERSIST,
    Hook.CONTEXT_COMPACT,
    Hook.MEMORY_SEARCH,
    Hook.APPROVAL_REQUEST,
    Hook.TURN_COMPLETE,
    Hook.TURN_CANCEL,
    Hook.TURN_RETRY,
)

#: The boolean assertions every `expect` object states. Required, all of them:
#: an omitted flag would read as a silent "no" rather than as the corpus fixing
#: an answer.
EXPECT_FLAGS: Final[tuple[str, ...]] = (
    "audit_record_required",
    "canonical_mutation_applied",
    "host_source_patched",
    "direct_storage_access",
    "capability_expanded",
    "core_run_state_persisted",
    "core_governance_decision_recorded",
    "implies_core_job_cancel",
    "implies_core_job_retry",
    "host_identity_in_request_metadata",
)

#: The optional strings an `expect` object may add, by disposition family.
_EXPECT_OPTIONAL: Final[tuple[str, ...]] = (
    "error_code",
    "retry_class",
    "compatibility_status",
)

_CASE_ID = re.compile(r"^SPI-V-\d{3}$")
_CASE_KEYS: Final[frozenset[str]] = frozenset(
    {"id", "title", "family", "hook", "requirements", "given", "when", "expect"}
)

#: The keys the accepted corpus states at its root. An unknown one is a corpus
#: carrying something this loader does not read, which is not the corpus it
#: accepts.
_ROOT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "fixture_id",
        "fixture_version",
        "status",
        "generated_date",
        "spi_version",
        "authority",
        "boundary",
        "hooks",
        "cases",
    }
)

_T = TypeVar("_T")


class CorpusError(ValueError):
    """The corpus on disk is not the corpus this loader accepts."""


def _freeze(value: object, field: str = "value") -> object:
    """Return `value` with every nested container replaced by an immutable one.

    Sets are refused rather than frozen: a `set` has no order to preserve, so
    any tuple this returned would be an order this function invented, and two
    equal corpora could then compare unequal. `frozenset` is refused for the
    same reason -- immutable, but still not ordered.
    """
    if isinstance(value, Mapping):
        items: Mapping[Any, Any] = value
        return MappingProxyType(
            {str(key): _freeze(item, field) for key, item in items.items()}
        )
    if isinstance(value, frozenset | set):
        raise TypeError(f"{field} must not hold a set: a set has no fixed order")
    if isinstance(value, list | tuple):
        entries: Sequence[Any] = value
        return tuple(_freeze(entry, field) for entry in entries)
    return value


def _mapping(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CorpusError(f"{where} is not an object")
    return {str(key): item for key, item in value.items()}


def _string(source: Mapping[str, Any], key: str, where: str) -> str:
    value = source.get(key)
    if not isinstance(value, str):
        raise CorpusError(f"{where} has no string {key!r}")
    return value


def _optional_string(source: Mapping[str, Any], key: str, where: str) -> str | None:
    if key not in source:
        return None
    value = source[key]
    if not isinstance(value, str):
        raise CorpusError(f"{where} has a non-string {key!r}")
    return value


# --- constructor guards ----------------------------------------------------------
#
# `frozen=True` freezes the attribute *bindings*, not what they are bound to: a
# caller passing a list or a plain dict gets a "frozen" instance handing out a
# container it can still edit, and a type hint does not stop it. Each public DTO
# therefore canonicalises what it was given in `__post_init__` -- sequences to
# tuples, mappings to mapping proxies over frozen values -- and rejects what
# cannot be canonicalised. Ordinary lists and dicts stay accepted; what does not
# survive construction is a *mutable* container reachable from the instance.


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _count(value: object, field: str) -> int:
    # `bool` is an `int` subclass; `passed=True` as a count is a caller mistake.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must not be negative")
    return value


def _sequence(value: object, kind: type[_T], field: str) -> tuple[_T, ...]:
    # A `str` is a `Sequence[str]`, so it would pass a per-entry check silently.
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise TypeError(f"{field} must be a sequence")
    entries = tuple(cast("Sequence[object]", value))
    if not all(isinstance(entry, kind) for entry in entries):
        raise TypeError(f"{field} must hold only {kind.__name__} entries")
    return cast("tuple[_T, ...]", entries)


def _frozen(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    items: Mapping[Any, Any] = value
    return MappingProxyType(
        {str(key): _freeze(item, field) for key, item in items.items()}
    )


@dataclass(frozen=True, slots=True)
class ExpectedOutcome:
    """What the corpus fixes as the right answer for one case.

    `flags` carries every entry of :data:`EXPECT_FLAGS` and nothing else, so a
    later executor reads an answer rather than an absence.
    """

    disposition: Disposition
    rationale: str
    flags: Mapping[str, bool]
    error_code: str | None = None
    retry_class: str | None = None
    compatibility_status: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, Disposition):
            raise TypeError("disposition must be a Disposition")
        _text(self.rationale, "rationale")
        if not isinstance(self.flags, Mapping):
            raise TypeError("flags must be a mapping")
        flags: Mapping[Any, Any] = self.flags
        given = {str(key): item for key, item in flags.items()}
        if set(given) != set(EXPECT_FLAGS):
            raise ValueError("flags must state exactly the expectation flags")
        if not all(isinstance(item, bool) for item in given.values()):
            raise TypeError("every expectation flag must be a boolean")
        object.__setattr__(
            self,
            "flags",
            MappingProxyType({flag: given[flag] for flag in EXPECT_FLAGS}),
        )
        for field in _EXPECT_OPTIONAL:
            item = getattr(self, field)
            if item is not None:
                _text(item, field)


@dataclass(frozen=True, slots=True)
class ConformanceCase:
    """One declarative case, exactly as the corpus states it."""

    id: str
    title: str
    family: str
    hook: Hook
    requirements: tuple[str, ...]
    given: Mapping[str, Any]
    when: Mapping[str, Any]
    expect: ExpectedOutcome

    def __post_init__(self) -> None:
        if not _CASE_ID.match(_text(self.id, "id")):
            raise ValueError(f"id is malformed: {self.id!r}")
        _text(self.title, "title")
        _text(self.family, "family")
        if not isinstance(self.hook, Hook):
            raise TypeError("hook must be a Hook")
        if not isinstance(self.expect, ExpectedOutcome):
            raise TypeError("expect must be an ExpectedOutcome")
        object.__setattr__(
            self, "requirements", _sequence(self.requirements, str, "requirements")
        )
        object.__setattr__(self, "given", _frozen(self.given, "given"))
        object.__setattr__(self, "when", _frozen(self.when, "when"))


@dataclass(frozen=True, slots=True)
class ConformanceCorpus:
    """The loaded corpus, structurally validated.

    Every root field the accepted corpus states is carried, not just the three
    identity ones: a loader that read `status`, `generated_date`, `authority`
    and `boundary` only to drop them would accept a corpus and then hand back
    less than it accepted. Their *shape* is fixed here -- two strings, two
    mappings -- and nothing more: what a status value means, and whether a
    boundary claim is true, is not this slice's question.
    """

    fixture_id: str
    fixture_version: str
    status: str
    generated_date: str
    spi_version: str
    authority: Mapping[str, Any]
    boundary: Mapping[str, Any]
    hooks: tuple[Hook, ...]
    cases: tuple[ConformanceCase, ...]

    def __post_init__(self) -> None:
        _text(self.fixture_id, "fixture_id")
        _text(self.fixture_version, "fixture_version")
        _text(self.status, "status")
        _text(self.generated_date, "generated_date")
        _text(self.spi_version, "spi_version")
        object.__setattr__(self, "authority", _frozen(self.authority, "authority"))
        object.__setattr__(self, "boundary", _frozen(self.boundary, "boundary"))
        object.__setattr__(self, "hooks", _sequence(self.hooks, Hook, "hooks"))
        object.__setattr__(
            self, "cases", _sequence(self.cases, ConformanceCase, "cases")
        )

    def case(self, case_id: str) -> ConformanceCase:
        for candidate in self.cases:
            if candidate.id == case_id:
                return candidate
        raise KeyError(case_id)

    @property
    def disposition_totals(self) -> Mapping[Disposition, int]:
        totals: dict[Disposition, int] = {}
        for case in self.cases:
            totals[case.expect.disposition] = totals.get(case.expect.disposition, 0) + 1
        return MappingProxyType(totals)


@dataclass(frozen=True, slots=True)
class CaseReport:
    """One case's recorded result, as an executing lane observed it.

    `observed` is what a provider actually answered -- `None` when the case did
    not reach a disposition at all -- and `passed` is the executor's verdict.
    Neither is derived here, and neither may be derived from `case_id`.
    """

    case_id: str
    passed: bool
    observed: Disposition | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not _CASE_ID.match(_text(self.case_id, "case_id")):
            raise ValueError(f"case_id is malformed: {self.case_id!r}")
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be a boolean")
        if self.observed is not None and not isinstance(self.observed, Disposition):
            raise TypeError("observed must be a Disposition or None")
        _text(self.detail, "detail")


@dataclass(frozen=True, slots=True)
class CorpusReport:
    """Every case's report, with totals that have to add up."""

    reports: tuple[CaseReport, ...]
    total: int
    passed: int
    failed: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "reports", _sequence(self.reports, CaseReport, "reports")
        )
        _count(self.total, "total")
        _count(self.passed, "passed")
        _count(self.failed, "failed")
        if self.total != len(self.reports):
            raise ValueError("total does not match the number of case reports")
        if self.passed + self.failed != self.total:
            raise ValueError("passed and failed do not sum to total")
        observed_pass = sum(1 for report in self.reports if report.passed)
        if observed_pass != self.passed:
            raise ValueError("passed does not match the case reports")
        identifiers = {report.case_id for report in self.reports}
        if len(identifiers) != len(self.reports):
            raise ValueError("a case is reported more than once")


def _load_expect(raw: object, where: str) -> ExpectedOutcome:
    source = _mapping(raw, where)
    unknown = set(source) - {
        "disposition",
        "rationale",
        *EXPECT_FLAGS,
        *_EXPECT_OPTIONAL,
    }
    if unknown:
        raise CorpusError(f"{where} states unknown keys: {sorted(unknown)}")
    disposition = _string(source, "disposition", where)
    try:
        parsed = Disposition(disposition)
    except ValueError as error:
        raise CorpusError(
            f"{where} names unknown disposition {disposition!r}"
        ) from error
    flags: dict[str, bool] = {}
    for flag in EXPECT_FLAGS:
        value = source.get(flag)
        if not isinstance(value, bool):
            raise CorpusError(f"{where} has no boolean {flag!r}")
        flags[flag] = value
    return ExpectedOutcome(
        disposition=parsed,
        rationale=_string(source, "rationale", where),
        flags=MappingProxyType(flags),
        error_code=_optional_string(source, "error_code", where),
        retry_class=_optional_string(source, "retry_class", where),
        compatibility_status=_optional_string(source, "compatibility_status", where),
    )


def _load_case(raw: object, position: int) -> ConformanceCase:
    where = f"case {position}"
    source = _mapping(raw, where)
    missing = _CASE_KEYS - set(source)
    if missing:
        raise CorpusError(f"{where} is missing {sorted(missing)}")
    unknown = set(source) - _CASE_KEYS
    if unknown:
        raise CorpusError(f"{where} states unknown keys: {sorted(unknown)}")
    case_id = _string(source, "id", where)
    if not _CASE_ID.match(case_id):
        raise CorpusError(f"{where} has a malformed id {case_id!r}")
    hook = _string(source, "hook", where)
    try:
        parsed_hook = Hook(hook)
    except ValueError as error:
        raise CorpusError(f"{case_id} names unknown hook {hook!r}") from error
    requirements = source["requirements"]
    if not isinstance(requirements, list) or not all(
        isinstance(item, str) for item in requirements
    ):
        raise CorpusError(f"{case_id} does not state requirements as strings")
    return ConformanceCase(
        id=case_id,
        title=_string(source, "title", where),
        family=_string(source, "family", where),
        hook=parsed_hook,
        requirements=tuple(str(item) for item in requirements),
        given=_mapping(source["given"], case_id),
        when=_mapping(source["when"], case_id),
        expect=_load_expect(source["expect"], f"{case_id} expect"),
    )


def load_corpus(path: Path) -> ConformanceCorpus:
    """Load and structurally validate the corpus file at `path`.

    Structural validation rather than JSON Schema validation, for the reason the
    connector kit gives: this package declares no third-party dependency, and a
    loader that imported an analyser would put it on the import path of every
    consumer of the public contract. The corpus ships its own `schema.json`,
    which a test validates against `jsonschema`.

    Every rejection this loader makes is a :class:`CorpusError`, an unreadable
    or unparseable file included: a caller handling "the corpus is not the one
    we accept" should not also have to catch `OSError` and `JSONDecodeError` to
    cover the same question.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError(f"the corpus at {path} could not be read: {error}") from error
    document = _mapping(raw, "the corpus")
    missing_keys = _ROOT_KEYS - set(document)
    if missing_keys:
        raise CorpusError(f"the corpus is missing {sorted(missing_keys)}")
    unknown_keys = set(document) - _ROOT_KEYS
    if unknown_keys:
        raise CorpusError(f"the corpus states unknown keys: {sorted(unknown_keys)}")
    fixture_id = _string(document, "fixture_id", "the corpus")
    if fixture_id != EXPECTED_FIXTURE_ID:
        raise CorpusError(f"the corpus is {fixture_id!r}, not {EXPECTED_FIXTURE_ID!r}")
    fixture_version = _string(document, "fixture_version", "the corpus")
    if fixture_version != EXPECTED_FIXTURE_VERSION:
        raise CorpusError(
            f"the corpus is version {fixture_version!r},"
            f" not {EXPECTED_FIXTURE_VERSION!r}"
        )
    spi_version = _string(document, "spi_version", "the corpus")
    if spi_version != SPI_VERSION:
        raise CorpusError(
            f"the corpus states SPI version {spi_version!r}, not {SPI_VERSION!r}"
        )
    # Shape only: a status string and a date string, an authority object and a
    # boundary object. What they say is the reading lane's business.
    status = _string(document, "status", "the corpus")
    generated_date = _string(document, "generated_date", "the corpus")
    authority = _mapping(document["authority"], "the corpus authority")
    boundary = _mapping(document["boundary"], "the corpus boundary")

    raw_hooks = document.get("hooks")
    if not isinstance(raw_hooks, list):
        raise CorpusError("the corpus does not state hooks as a list")
    hooks: list[Hook] = []
    for entry in raw_hooks:
        if not isinstance(entry, str):
            raise CorpusError("the corpus states a non-string hook")
        try:
            hooks.append(Hook(entry))
        except ValueError as error:
            raise CorpusError(f"the corpus names unknown hook {entry!r}") from error
    if tuple(hooks) != EXPECTED_HOOKS:
        raise CorpusError("the corpus does not state the ten hooks once each, in order")

    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list):
        raise CorpusError("the corpus does not state cases as a list")
    cases = tuple(
        _load_case(entry, position) for position, entry in enumerate(raw_cases)
    )
    if len(cases) != EXPECTED_CASE_COUNT:
        raise CorpusError(
            f"the corpus carries {len(cases)} cases, not {EXPECTED_CASE_COUNT}"
        )
    identifiers = tuple(case.id for case in cases)
    if len(set(identifiers)) != len(identifiers):
        raise CorpusError("the corpus repeats a case identifier")
    if identifiers != EXPECTED_CASE_IDS:
        raise CorpusError(
            "the corpus does not carry exactly SPI-V-001..SPI-V-042 in order"
        )

    corpus = ConformanceCorpus(
        fixture_id=fixture_id,
        fixture_version=fixture_version,
        status=status,
        generated_date=generated_date,
        spi_version=spi_version,
        authority=authority,
        boundary=boundary,
        hooks=tuple(hooks),
        cases=cases,
    )
    if dict(corpus.disposition_totals) != dict(EXPECTED_DISPOSITION_TOTALS):
        raise CorpusError("the corpus does not carry the accepted disposition totals")
    return corpus
