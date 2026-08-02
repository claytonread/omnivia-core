"""The canonical service probe router (P2.1).

A probe answers a transport-level question -- *is this process alive, is it ready,
what is it* -- and must be answerable before the concepts an application request
depends on exist. There is no workspace selected yet, no caller authenticated, and
no authority negotiated, so this router deliberately sits outside `RequestEnvelope`
and outside the application operation registry entirely. It speaks only the public
`ServiceProbeRequest` / `ServiceProbeResult` pair.

Exactly three probe kinds are answered: `service.health`, `service.readiness` and
`service.discover`. Nothing else is accepted -- not `core.health`,
`core.readiness` or `core.discovery` (the names the earlier in-process operation
registry used), and not a shortened spelling such as `health` or `discover`. Those
were operation names dispatched through the application path; these are probe
kinds outside it, and quietly accepting the old spellings here would re-create the
confusion between the two that the frozen contract exists to end.

Every fact a result reports is **injected**, never fetched. The router imports no
storage, lease, ownership or transport module, and reads no clock of its own: the
caller supplies a snapshot of current facts, the capabilities its registered
application handlers actually provide, and a monotonic clock. That is what keeps a
probe answerable when the subsystems it reports on are exactly what is broken.

Injecting a provider makes its *failure* part of this boundary too. A provider that
cannot reach what it describes raises its own exception, and that exception's text
is exactly the class of value this module exists to keep off the wire: `unable to
open database file /Users/.../workspace.sqlite3`, or a lease error quoting the token
it tried. So a provider that raises is answered with a fixed refusal naming only
*which* provider did not answer, and the original is attached as neither `__cause__`
nor `__context__`. `raise ... from None` is not enough for that second half: it
clears `__cause__` and merely suppresses the *display* of `__context__`, leaving the
original exception -- with its text -- reachable on the error a caller catches. The
translation therefore happens after the handler has exited, where there is no
current exception to chain.

The injected clock is held to the same standard, one step earlier. Its readings are
arithmetic operands, so a reading that is not an integer, or a second reading behind
the first, is refused before the subtraction rather than after: a wrong type would
escape as a `TypeError` from below this boundary, and a backwards clock would
silently produce a negative elapsed time -- an answer that looks comfortably inside
a budget it was never measured against.

Injected does not mean forwarded. A probe answers an **unauthenticated** caller, so
what leaves here is a deliberately narrow projection of the snapshot rather than the
snapshot itself: the frozen public status facts, and nothing else. Concretely, a
component is republished as its `id`, `status` and `observed_at` and nothing more,
and a result's `details` carries the caller's own request id or is absent. The free
text a subsystem attaches to its own status -- `ServiceComponentStatus.message` and
its open `details` object -- is dropped, and there is no field on `ServiceFacts` for
arbitrary details to arrive in at all.

That is a projection by allowlist, deliberately, and not a filter that inspects
values for secrets. A denylist has to recognize what it is looking at, and the
things that end up in a diagnostic string -- a database path, a lease holder's pid,
a connection string, an exception's text, a caller's own message -- do not share a
shape that can be recognized. Which fields are safe to publish is knowable; which
values are is not.

The allowlist decides which *fields* travel. It does not, on its own, decide that
what arrives in one is the kind of value that field is defined to hold, and that
gap is the second half of the same guarantee. `ServiceComponentStatus.id` is an
`Identifier`, whose frozen grammar admits no `/`, no space, no `@`, no `=` and no
control character; a Runtime that puts a database path, a connection string or a
half-megabyte of text there is not publishing a dangerous `Identifier`, it is
publishing something that is not an `Identifier` at all. So every provider-supplied
value is checked against its own public grammar and bound before it is published,
and a value outside it is refused rather than trimmed, escaped or re-spelled.

That stays grammar enforcement, not secret-detection, and the distinction is what
keeps it honest: nothing here asks whether a value *looks* sensitive. It asks only
whether the value is one the contract says that field can hold. A path is refused
from `ServiceComponentStatus.id` because an `Identifier` cannot contain one -- and
the very same path is published without complaint as `endpoint_uri`, which P0 froze
as the coordination fact a client needs to dial the instance at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, TypeAlias, TypeVar

from omnivia_core.contracts.v1 import (
    CAPABILITY_ID_PATTERN,
    CONTRACT_VERSION_PATTERN,
    IDENTIFIER_PATTERN,
    OPEN_CODE_PATTERN,
    PROBE_STATUS_PATTERN,
    RELEASE_VERSION_PATTERN,
    REQUEST_ID_PATTERN,
    TIMESTAMP_PATTERN,
    WORKSPACE_ID_PATTERN,
    CapabilityRef,
    ContractSemanticError,
    ServiceComponentStatus,
    ServiceEndpointDescriptor,
    ServiceProbeRequest,
    ServiceProbeResult,
    ServiceProcessEvidence,
    VersionWindow,
    duplicate_capability_ids,
    validate_version_window,
)
from omnivia_core_runtime.service.versions import API_VERSION, SERVER_VERSION

PROBE_HEALTH: Final = "service.health"
PROBE_READINESS: Final = "service.readiness"
PROBE_DISCOVER: Final = "service.discover"

#: The complete set of probe kinds this build answers. A kind outside it is
#: refused; it is never treated as an alias for one inside it.
CANONICAL_PROBES: Final[frozenset[str]] = frozenset(
    {PROBE_HEALTH, PROBE_READINESS, PROBE_DISCOVER}
)

#: The `details` key a request id is echoed under. `ServiceProbeResult` has no
#: dedicated field for it, and inventing a top-level one would put a value on the
#: wire the accepted DTO does not define.
REQUEST_ID_DETAIL: Final = "request_id"

#: The component fields a probe publishes, and the whole of them.
#:
#: `ServiceComponentStatus` also accepts a free-text `message` and an open `details`
#: object. Neither is here, and neither is republished: which subsystem, how it is
#: doing, and when that was observed is the entire public health and readiness
#: semantic, and it is enough to decide whether to route traffic at an instance.
#: Anything richer is a diagnostic, and a diagnostic is for an operator reading this
#: build's own logs, not for whoever opened a socket.
PUBLIC_COMPONENT_FIELDS: Final[tuple[str, ...]] = ("id", "status", "observed_at")

#: Nanoseconds per millisecond. Deadlines are stated in milliseconds by the
#: contract and measured in monotonic nanoseconds here, so the comparison is exact
#: integer arithmetic rather than float seconds.
_NS_PER_MS: Final = 1_000_000

# --- the public bounds a published value has to fit -------------------------
#
# The patterns come from the contract's own exported constants, so this module and
# the schema cannot describe two different languages. The *lengths* are not
# exported alongside them, so they are pinned here against the packaged schema and
# held there by `test_versions_and_probes.py`, which reads
# `contracts/application/v1/schemas` and fails if either side moves. Restating a
# bound is a real risk; restating it with a test reading the canonical document is
# the cheapest way to make the restatement provably faithful.

#: `maxLength` of the 128-bounded scalars published here: `Identifier`,
#: `WorkspaceId`, `CapabilityId`, `ProbeStatus`, `OpenCode`, `RequestId`,
#: `ReleaseVersion`, and `ServiceProcessEvidence.start_time`.
_BOUNDED_MAX: Final = 128

#: `ContractVersion.maxLength`. Narrower than the rest: `major.minor` and nothing.
_CONTRACT_VERSION_MAX: Final = 32

#: `Timestamp.maxLength`, wide enough for nine fractional digits and no wider.
_TIMESTAMP_MAX: Final = 40

#: `ServiceEndpointDescriptor.endpoint_uri.maxLength`.
_ENDPOINT_URI_MAX: Final = 2048

#: `CapabilityId.minLength`: `a.b` is the shortest namespaced capability there is.
_CAPABILITY_ID_MIN: Final = 3

#: `maxItems` on both published arrays -- `components` and
#: `supported_capabilities`. A snapshot naming more subsystems than the contract
#: publishes is refused rather than truncated: a silently shortened list reads as a
#: complete one, and a caller deciding whether to route traffic here would be
#: deciding on a subset nothing told it was a subset.
_MAX_PUBLISHED_ITEMS: Final = 256

#: `DurationMs.maximum`, in milliseconds: twenty-four hours.
_DURATION_MS_MAX: Final = 86_400_000

#: One frozen scalar type: the grammar its values must fullmatch, and the inclusive
#: length bounds they must fit. Kept as data rather than one function per type so
#: every call site reads as "this field holds one of these", and so a field that
#: gains a bound gains it in one place.
_Scalar: TypeAlias = tuple[re.Pattern[str], int, int]

_IDENTIFIER: Final[_Scalar] = (re.compile(IDENTIFIER_PATTERN), 1, _BOUNDED_MAX)
_WORKSPACE_ID: Final[_Scalar] = (re.compile(WORKSPACE_ID_PATTERN), 1, _BOUNDED_MAX)
_PROBE_STATUS: Final[_Scalar] = (re.compile(PROBE_STATUS_PATTERN), 1, _BOUNDED_MAX)
_OPEN_CODE: Final[_Scalar] = (re.compile(OPEN_CODE_PATTERN), 1, _BOUNDED_MAX)
_REQUEST_ID: Final[_Scalar] = (re.compile(REQUEST_ID_PATTERN), 1, _BOUNDED_MAX)
_RELEASE_VERSION: Final[_Scalar] = (
    re.compile(RELEASE_VERSION_PATTERN),
    1,
    _BOUNDED_MAX,
)
_CONTRACT_VERSION: Final[_Scalar] = (
    re.compile(CONTRACT_VERSION_PATTERN),
    1,
    _CONTRACT_VERSION_MAX,
)
_CAPABILITY_ID: Final[_Scalar] = (
    re.compile(CAPABILITY_ID_PATTERN),
    _CAPABILITY_ID_MIN,
    _BOUNDED_MAX,
)
_TIMESTAMP: Final[_Scalar] = (re.compile(TIMESTAMP_PATTERN), 1, _TIMESTAMP_MAX)

#: `ServiceProcessEvidence.start_time` is the one published string the schema
#: bounds without patterning: its spelling is whatever the host platform reports,
#: so there is no grammar to hold it to. Control characters are still refused --
#: a value carrying a newline or an ANSI escape is not a platform reading, it is
#: something that will be pasted into an operator's terminal.
_OPAQUE_TEXT: Final[_Scalar] = (
    re.compile(r"[^\x00-\x1f\x7f-\x9f]+"),
    1,
    _BOUNDED_MAX,
)

#: An absolute URI: an RFC 3986 scheme, then only characters RFC 3986 permits,
#: percent-encodings included and only where they are well formed. `endpoint_uri`
#: is the one published string the schema constrains by `format` rather than by
#: pattern, and `format: uri` is exactly what a runtime with no URI library cannot
#: check by accident -- so it is checked on purpose here. Deliberately narrower
#: than a permissive parser at one point: a trailing newline is refused, because a
#: control character has no business on a wire an unauthenticated caller reads.
_ENDPOINT_URI_RE: Final = re.compile(
    r"[A-Za-z][A-Za-z0-9+.\-]*:"
    r"(?:[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=]|%[0-9A-Fa-f]{2})*"
)


class ProbeError(Exception):
    """A probe could not be answered.

    Carries no secret, no filesystem or database location, no lease or ownership
    internal, and no server-side value at all: a message names the *field* that was
    unusable, never its content. A probe is answered before authentication, so
    anything it says is said to an unauthenticated caller.
    """


class ProbeDeadlineExceeded(ProbeError):
    """The caller's relative deadline elapsed before the probe could be answered.

    Distinct from a malformed request because it means something different to the
    transport that will later sit above this: nothing is wrong with the request or
    with this build, the budget simply ran out.
    """


@dataclass(frozen=True, slots=True)
class ServiceFacts:
    """One immutable snapshot of what this instance currently knows about itself.

    Supplied by the caller rather than gathered here. `components` describes the
    health and readiness probes; `descriptor` describes discovery and is never
    attached to the other two, because health and readiness answer questions about
    a running process, not about where to reach it.

    There is deliberately no field for free-form details. An earlier shape had one,
    and it was the shortest path from a Runtime that knows a backend path or a lease
    holder to an unauthenticated caller that is told them. Removing the field is what
    makes that impossible rather than merely discouraged: a component's `message` and
    `details` may still be populated -- they are useful to whoever is logging them --
    but they are dropped by the projection below and never reach the wire.
    """

    observed_at: str
    health_status: str
    readiness_status: str
    discovery_status: str
    components: tuple[ServiceComponentStatus, ...] = ()
    descriptor: ServiceEndpointDescriptor | None = None


#: Supplies the current snapshot. A callable rather than a stored value so a probe
#: reports the state at the moment it is answered, not at construction.
FactsProvider = Callable[[], ServiceFacts]

#: Supplies the capabilities this build's *registered* application handlers
#: provide. Injected so discovery can never fall back to advertising the whole
#: operation catalogue: an accepted contract entry with no handler behind it is
#: not a capability this build supports.
CapabilityProvider = Callable[[], Sequence[CapabilityRef]]

#: A monotonic clock in nanoseconds, e.g. `time.monotonic_ns`. Injected, and
#: monotonic rather than wall-clock, so a system clock adjustment cannot make a
#: deadline appear to have already passed.
MonotonicClock = Callable[[], int]

#: What one injected provider hands back. Only ever bound to the return type of
#: the callable :func:`_provided` was handed, so guarding a provider costs the
#: call site nothing in type precision.
_Provided = TypeVar("_Provided")


def _provided(call: Callable[[], _Provided], refusal: str) -> _Provided:
    """Call one injected provider, or refuse with `refusal` and nothing else.

    The single place a provider is ever invoked. A probe is answered *because*
    something is broken, so a provider raising is the expected case rather than
    the surprising one -- and what it raises is written for whoever reads this
    build's logs. `OperationalError: unable to open database file /Users/.../
    workspace.sqlite3` names the backend's location; a lease failure names the
    holder; an auth failure quotes the credential it was handed. Letting any of
    them escape here hands it to an unauthenticated caller, and does so through
    the one path the field allowlist above does not cover, because a raised
    exception is not a published field.

    So the refusal is a fixed string chosen by the call site, and it says which
    provider did not answer. Not why, not what it said, and not what it
    returned: none of that has been vetted, and a probe boundary cannot vet it.

    The original is dropped rather than chained, and *that* is why the refusal
    is raised after the handler rather than inside it. `raise ... from None`
    only looks equivalent: it clears `__cause__` and sets `__suppress_context__`
    so a rendered traceback stays quiet, but `__context__` still references the
    provider's exception, and `error.__context__` is one attribute access away
    for anything that logs, serializes or reports the error a caller catches.
    Raising once the `except` block has exited means there is no exception being
    handled, so neither attribute is set at all.

    `BaseException` is deliberately not caught. `KeyboardInterrupt`,
    `SystemExit` and `GeneratorExit` are the interpreter unwinding this process,
    not a provider reporting that a subsystem is unwell, and converting one into
    a probe refusal would swallow a shutdown rather than sanitize a leak.
    """
    try:
        return call()
    except Exception:  # noqa: BLE001, S110 -- deliberate; see the docstring
        # Blind by necessity: arbitrary injected code raises arbitrary
        # exceptions, and one that is not caught here is one that escapes with
        # its text intact. Nothing is logged because this module has nowhere to
        # log to, and the Runtime that owns the provider is the one placed to
        # record why its own subsystem failed. The `pass` is load-bearing: it
        # ends the handler, so the refusal below is raised with no exception
        # being handled and therefore nothing to chain to.
        pass
    raise ProbeError(refusal)


@dataclass(frozen=True)
class ProbeRouter:
    """Answers the three canonical probes from injected facts."""

    facts: FactsProvider
    capabilities: CapabilityProvider
    clock: MonotonicClock

    def route(self, request: ServiceProbeRequest) -> ServiceProbeResult:
        """Answer one probe, or raise :class:`ProbeError`.

        The request is checked before anything is read from it, including the
        clock. `ServiceProbeRequest` is a plain frozen dataclass, so a caller that
        builds one directly rather than decoding it can put anything in any field,
        and every operation below -- a set membership, an integer comparison, a
        regex -- has a wrong-type input that raises something other than a
        :class:`ProbeError`. Those are the same refusal to the caller and a
        different exception to whatever is above this, so they are turned into the
        boundary error here rather than left to escape as `TypeError`.

        The deadline is checked twice and owned neither time: once up front, so a
        request that arrives with no budget left is refused before any work is
        done, and once after the facts are assembled, so an answer that took
        longer than the caller allowed is not returned as if it were timely.
        Enforcing an actual timeout stays with the transport that owns the
        connection; all this does is refuse to answer past the budget.

        Every provider below is reached through :func:`_provided`, so a provider
        that raises refuses this probe rather than escaping through it.
        """
        _validate_request(request)
        started = self._clock_reading()
        deadline_ms = request.deadline_ms
        if deadline_ms is not None and deadline_ms <= 0:
            raise ProbeDeadlineExceeded(
                f"probe {request.probe!r} carries no time budget: deadline_ms is "
                f"{deadline_ms}"
            )

        probe = request.probe
        if probe not in CANONICAL_PROBES:
            known = ", ".join(sorted(CANONICAL_PROBES))
            raise ProbeError(
                f"unknown probe {probe!r}: this build answers only {known}"
            )

        facts = _provided(self.facts, "service facts are unavailable")
        _validate_facts(facts)
        result = self._build(probe, request, facts)
        self._check_deadline(started, deadline_ms, probe)
        return result

    def _build(
        self,
        probe: str,
        request: ServiceProbeRequest,
        facts: ServiceFacts,
    ) -> ServiceProbeResult:
        details = _public_details(request)
        if probe == PROBE_DISCOVER:
            # Only the *call* is guarded. `_validated_capabilities` raises this
            # module's own already-sanitized refusals, naming the entry that was
            # unpublishable, and folding those into the generic one would lose a
            # message that was safe to say and useful to whoever injected it.
            capabilities = _validated_capabilities(
                _provided(self.capabilities, "supported capabilities are unavailable")
            )
            return ServiceProbeResult(
                probe=probe,
                status=facts.discovery_status,
                server_version=SERVER_VERSION,
                api_version=API_VERSION,
                observed_at=facts.observed_at,
                supported_capabilities=capabilities,
                descriptor=facts.descriptor,
                details=details,
            )

        status = (
            facts.health_status if probe == PROBE_HEALTH else facts.readiness_status
        )
        return ServiceProbeResult(
            probe=probe,
            status=status,
            server_version=SERVER_VERSION,
            api_version=API_VERSION,
            observed_at=facts.observed_at,
            components=_public_components(facts.components),
            details=details,
        )

    def _clock_reading(self) -> int:
        """One monotonic reading, guarded and proven to be an integer.

        `MonotonicClock` says nanoseconds as an `int`, and an injected callable
        is under no obligation to keep that promise. The reading is an operand
        -- subtracted from, compared against a budget -- so a wrong type is not
        a wrong answer, it is a `TypeError` raised from inside the arithmetic,
        below the boundary that owns every refusal here.

        `bool` is excluded for the same reason it is in :func:`_bounded_int`: it
        is an `int` in Python, so a clock stubbed out to return `False` would
        otherwise read as the instant `0` and quietly make every deadline
        arithmetic sound.
        """
        reading: object = _provided(self.clock, "the monotonic clock is unavailable")
        if isinstance(reading, bool) or not isinstance(reading, int):
            raise ProbeError("the monotonic clock did not read an integer")
        return reading

    def _check_deadline(
        self, started: int, deadline_ms: int | None, probe: str
    ) -> None:
        # Still read exactly once when nothing asked for a budget: a probe with
        # no deadline is not measured, so a second reading -- and a clock that
        # could fail on it -- is work a caller did not ask for.
        if deadline_ms is None:
            return
        finished = self._clock_reading()
        if finished < started:
            # Not a deadline failure: a monotonic clock that ran backwards is
            # broken, and the caller's budget was never actually measured. The
            # subtraction would answer with a negative elapsed time, which is
            # below every budget there is -- so the probe would be published as
            # comfortably timely on the strength of a clock that cannot be
            # trusted to have timed it.
            raise ProbeError("the monotonic clock went backwards")
        if finished - started >= deadline_ms * _NS_PER_MS:
            raise ProbeDeadlineExceeded(
                f"probe {probe!r} exceeded its {deadline_ms}ms budget"
            )


def _public_details(request: ServiceProbeRequest) -> Mapping[str, Any] | None:
    """The result's `details`: the caller's own request id, echoed, or nothing.

    `ServiceProbeResult` echoes `probe` in a field of its own but has nowhere to
    put a request id, so the id is echoed under `details` -- the one place the
    accepted DTO permits it -- and only when the request carried one.

    Nothing server-side is merged in. The value returned here came from the caller
    it is returned to, one request ago, which is what makes it publishable before
    that caller has authenticated; a server-side observation would not be, and
    `details` is the field where one would otherwise be tempting to put.
    """
    if request.request_id is None:
        return None
    return {REQUEST_ID_DETAIL: request.request_id}


def _public_components(
    components: tuple[ServiceComponentStatus, ...],
) -> tuple[ServiceComponentStatus, ...] | None:
    """Project the injected components onto :data:`PUBLIC_COMPONENT_FIELDS`.

    Rebuilt field by field rather than forwarded. Forwarding is what made the
    difference invisible: the injected value and the published value were the same
    object, so a `message` a Runtime added for its own logs became something an
    unauthenticated caller was told, with no line of code anywhere deciding that.
    Rebuilding makes publication the explicit act it should be -- a field not named
    here is not published, whatever a future Runtime puts in it.

    `None` rather than an empty tuple when there is nothing to report, so the
    accepted DTO omits the field instead of stating an empty list of subsystems.
    """
    if not components:
        return None
    return tuple(
        ServiceComponentStatus(
            id=component.id,
            status=component.status,
            observed_at=component.observed_at,
        )
        for component in components
    )


def _scalar(value: object, label: str, scalar: _Scalar) -> str:
    """Return `value` when it is a publishable instance of `scalar`, else refuse.

    The type is narrowed before the length is measured and the length before the
    pattern is matched, in that order and never any other. `re.fullmatch` raises
    `TypeError` on a non-string and a pattern walks the whole of whatever it is
    given, so checking the grammar of an unvetted value is both the way a wrong
    type escapes as the wrong exception and the way an oversized one is paid for
    twice.

    A message names the field and stops. Not the value, not its length, not the
    grammar it failed: a probe answers before authentication, so the whole of what
    this can safely say is *which* field a caller's snapshot got wrong.
    """
    pattern, minimum, maximum = scalar
    if not isinstance(value, str):
        raise ProbeError(f"{label} is not a string")
    if not minimum <= len(value) <= maximum:
        raise ProbeError(f"{label} is malformed")
    if pattern.fullmatch(value) is None:
        raise ProbeError(f"{label} is malformed")
    return value


def _timestamp(value: object, label: str) -> None:
    """Refuse anything that is not a real instant spelled as a `Timestamp`.

    A pattern is not a calendar. `2026-13-45T99:99:99Z` satisfies
    `TIMESTAMP_PATTERN` character for character and names no moment that has ever
    existed, so the pattern is the first half of the check and parsing is the
    second -- the same two halves the contract's own conformance checker applies to
    a declared `format: date-time`.

    Parsing is not clock-reading. This reads a value the caller handed over; the
    router still owns no clock, and the only time it knows is the monotonic one
    injected for deadlines.
    """
    text = _scalar(value, label, _TIMESTAMP)
    try:
        datetime.fromisoformat(f"{text[:-1]}+00:00")
    except ValueError:
        raise ProbeError(f"{label} is malformed") from None


def _endpoint_uri(value: object, label: str) -> None:
    """Refuse an endpoint that is not a bounded, credential-free absolute URI.

    `endpoint_uri` is the one published field that is *meant* to carry a path: a
    unix socket lives somewhere on a filesystem and a client cannot dial it without
    being told where. So the check here is not "is this a path" -- it is that the
    value is a URI at all, that it fits, and that its authority carries no
    `userinfo`.

    The `userinfo` rule is the one refusal here that is about content, and it is
    structural rather than a guess at what a secret looks like: `user:password@` is
    a *component* of an RFC 3986 authority, recognized by position, not a string
    that resembles a credential. `ServiceEndpointDescriptor` states outright that a
    descriptor carries no bearer credential or token, and an endpoint spelled
    `http://svc:hunter2@127.0.0.1:9000` hands one to every unauthenticated caller
    that asks what this instance is.
    """
    if not isinstance(value, str):
        raise ProbeError(f"{label} is not a string")
    if not 1 <= len(value) <= _ENDPOINT_URI_MAX:
        raise ProbeError(f"{label} is malformed")
    if _ENDPOINT_URI_RE.fullmatch(value) is None:
        raise ProbeError(f"{label} is malformed")
    if "@" in _uri_authority(value):
        raise ProbeError(f"{label} carries a userinfo component")


def _uri_authority(uri: str) -> str:
    """The RFC 3986 `authority` of `uri`, or the empty string when it has none."""
    _, _, rest = uri.partition(":")
    if not rest.startswith("//"):
        return ""
    authority = rest[2:]
    for delimiter in ("/", "?", "#"):
        authority = authority.partition(delimiter)[0]
    return authority


def _bounded_int(value: object, label: str, minimum: int) -> None:
    """Refuse a non-integer, and an integer below the contract's own minimum.

    `bool` is excluded explicitly. It is an `int` in Python and is not one in JSON,
    so `ready=True` would otherwise satisfy a `fencing_generation` check and be
    published as the generation `1`.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProbeError(f"{label} is not an integer")
    if value < minimum:
        raise ProbeError(f"{label} is out of range")


def _version_window(window: object, label: str) -> None:
    """Refuse a window that is not two contract versions in negotiable order."""
    if not isinstance(window, VersionWindow):
        raise ProbeError(f"{label} is not a version window")
    _scalar(window.minimum, f"{label} minimum", _CONTRACT_VERSION)
    _scalar(window.maximum, f"{label} maximum", _CONTRACT_VERSION)
    try:
        validate_version_window(window)
    except ContractSemanticError:
        # Refused with a message of this module's own, and `from None` so the
        # contract's is not carried along either. The contract's text quotes the
        # bounds it rejected, which is right for a library raising to its caller
        # and wrong here: nothing nested is vetted for what an unauthenticated
        # caller may be told, so nothing nested is passed on.
        raise ProbeError(f"{label} is malformed") from None


def _validate_request(request: ServiceProbeRequest) -> None:
    """Refuse a probe request that could not be answered as one.

    Only the fields this router acts on, and each one before it is acted on. The
    probe kind is proven to be a bounded string here and its *value* is judged in
    :meth:`ProbeRouter.route`, where the answer is that it names no probe this
    build has; an unhashable kind would otherwise fail the membership test with a
    `TypeError` before reaching that answer.

    The kind is bounded and not patterned, deliberately. `route` quotes it back --
    naming the spelling that was refused is most of what makes that refusal useful,
    and it is the caller's own string returned to the caller it came from -- but
    quoting an unbounded one turns a one-line refusal into whatever length the
    sender chose. `ProbeKind.maxLength` is the bound, and a near-miss spelling
    inside it is still answered as an unknown probe rather than a malformed one.

    `request_id` is held to `RequestId` because it is the one caller-supplied value
    that is published back: it is echoed into `details`, so a non-string or an
    overlong one would put a value on the wire that `ServiceProbeResult` does not
    describe. A non-positive `deadline_ms` is deliberately *not* refused here --
    that is a spent budget, not a malformed request, and route answers it as one.
    """
    if not isinstance(request, ServiceProbeRequest):
        raise ProbeError(
            f"probe request is not a ServiceProbeRequest, got {type(request).__name__}"
        )
    if not isinstance(request.probe, str):
        raise ProbeError("probe request names a probe kind that is not a string")
    if len(request.probe) > _BOUNDED_MAX:
        raise ProbeError("probe request names a probe kind that is out of range")
    if request.request_id is not None:
        _scalar(request.request_id, "probe request request_id", _REQUEST_ID)

    deadline_ms = request.deadline_ms
    if deadline_ms is None:
        return
    if isinstance(deadline_ms, bool) or not isinstance(deadline_ms, int):
        raise ProbeError("probe request deadline_ms is not an integer")
    if deadline_ms > _DURATION_MS_MAX:
        raise ProbeError("probe request deadline_ms is out of range")


def _validate_facts(facts: ServiceFacts) -> None:
    """Refuse a snapshot that could not produce a valid result.

    Checked as a whole rather than per probe. The snapshot is one value produced
    by one caller; if part of it is malformed the caller has a bug, and answering
    the probes that happen not to read the broken field would hide it.

    Every field is checked, not only the ones an earlier candidate happened to
    look at. `ServiceComponentStatus.id` was the gap that mattered: it was
    published verbatim and never checked, so a subsystem naming itself with the
    path to the database it could not open was telling an unauthenticated caller
    where that database lives.
    """
    if not isinstance(facts, ServiceFacts):
        raise ProbeError("service facts are not a ServiceFacts snapshot")
    _timestamp(facts.observed_at, "service facts observed_at")
    for label, status in (
        ("health_status", facts.health_status),
        ("readiness_status", facts.readiness_status),
        ("discovery_status", facts.discovery_status),
    ):
        _scalar(status, f"service facts {label}", _PROBE_STATUS)

    components = facts.components
    # Narrowed before it is walked. `enumerate` raises on an int and silently
    # succeeds on a string, which is how a snapshot naming one component ends up
    # reported as one component per character.
    if not isinstance(components, tuple):
        raise ProbeError("service facts components are not a tuple")
    if len(components) > _MAX_PUBLISHED_ITEMS:
        raise ProbeError(
            f"service facts carry more than {_MAX_PUBLISHED_ITEMS} components"
        )
    for index, component in enumerate(components):
        if not isinstance(component, ServiceComponentStatus):
            raise ProbeError(
                f"service facts component {index} is not a component status"
            )
        where = f"service facts component {index}"
        _scalar(component.id, f"{where} id", _IDENTIFIER)
        _scalar(component.status, f"{where} status", _PROBE_STATUS)
        _timestamp(component.observed_at, f"{where} observed_at")

    descriptor = facts.descriptor
    if descriptor is None:
        return
    if not isinstance(descriptor, ServiceEndpointDescriptor):
        raise ProbeError(
            "service facts carry a descriptor that is not an endpoint descriptor"
        )
    _validate_descriptor(descriptor)


def _validate_descriptor(descriptor: ServiceEndpointDescriptor) -> None:
    """Refuse a descriptor discovery cannot publish as it stands.

    Every field, because every field is published. The descriptor is the one
    injected value that travels whole rather than through a projection -- P0 froze
    its shape, and its shape *is* the public one -- so there is no allowlist here
    doing half the work, and checking it is the whole of what stands between a
    Runtime's mistake and an unauthenticated caller.
    """
    label = "service facts descriptor"
    _scalar(
        descriptor.descriptor_version, f"{label} descriptor_version", _CONTRACT_VERSION
    )
    _scalar(descriptor.workspace_id, f"{label} workspace_id", _WORKSPACE_ID)
    _scalar(descriptor.service_instance_id, f"{label} service_instance_id", _IDENTIFIER)
    _scalar(descriptor.installation_id, f"{label} installation_id", _IDENTIFIER)
    _endpoint_uri(descriptor.endpoint_uri, f"{label} endpoint_uri")
    _scalar(descriptor.protocol_version, f"{label} protocol_version", _CONTRACT_VERSION)
    _scalar(descriptor.server_version, f"{label} server_version", _RELEASE_VERSION)
    _version_window(
        descriptor.supported_api_versions, f"{label} supported_api_versions"
    )
    _version_window(
        descriptor.supported_workspace_versions, f"{label} supported_workspace_versions"
    )
    _scalar(
        descriptor.workspace_format_version,
        f"{label} workspace_format_version",
        _CONTRACT_VERSION,
    )
    if not isinstance(descriptor.ready, bool):
        raise ProbeError(f"{label} ready is not a boolean")
    _scalar(descriptor.lifecycle_state, f"{label} lifecycle_state", _OPEN_CODE)
    _bounded_int(descriptor.fencing_generation, f"{label} fencing_generation", 1)
    _timestamp(descriptor.published_at, f"{label} published_at")

    process = descriptor.process
    if process is None:
        return
    if not isinstance(process, ServiceProcessEvidence):
        raise ProbeError(f"{label} process is not process evidence")
    _bounded_int(process.pid, f"{label} process pid", 1)
    _scalar(process.start_time, f"{label} process start_time", _OPAQUE_TEXT)
    _scalar(process.boot_id, f"{label} process boot_id", _IDENTIFIER)


def _validated_capabilities(
    capabilities: Sequence[CapabilityRef],
) -> tuple[CapabilityRef, ...]:
    """Refuse a capability inventory discovery cannot honestly advertise.

    These are `supported` capabilities and nothing else. An unauthenticated probe
    has no caller and no workspace to authorize against, so it must never state
    granted or effective authority -- there is no field here that could, and there
    is no computation here that would.

    Each reference is proven to hold a real `CapabilityId` and `ContractVersion`
    before the inventory is checked for duplicates, and that order is not
    incidental: `duplicate_capability_ids` counts ids in a dict, so an id that is
    not a string reaches it as an unhashable key and fails with a `TypeError`
    raised from inside the contract package.
    """
    if isinstance(capabilities, str | bytes) or not isinstance(capabilities, Sequence):
        raise ProbeError("supported capabilities are not a sequence")
    refs = tuple(capabilities)
    if len(refs) > _MAX_PUBLISHED_ITEMS:
        raise ProbeError(
            f"supported capabilities name more than {_MAX_PUBLISHED_ITEMS} entries"
        )
    for index, ref in enumerate(refs):
        if not isinstance(ref, CapabilityRef):
            raise ProbeError(
                f"supported capability {index} is not a capability reference"
            )
        _scalar(ref.id, f"supported capability {index} id", _CAPABILITY_ID)
        _scalar(ref.version, f"supported capability {index} version", _CONTRACT_VERSION)
    duplicates = duplicate_capability_ids(refs)
    if duplicates:
        raise ProbeError(
            f"supported capabilities name {len(duplicates)} id(s) more than once"
        )
    return refs


__all__ = [
    "CANONICAL_PROBES",
    "PROBE_DISCOVER",
    "PROBE_HEALTH",
    "PROBE_READINESS",
    "PUBLIC_COMPONENT_FIELDS",
    "REQUEST_ID_DETAIL",
    "CapabilityProvider",
    "FactsProvider",
    "MonotonicClock",
    "ProbeDeadlineExceeded",
    "ProbeError",
    "ProbeRouter",
    "ServiceFacts",
]
