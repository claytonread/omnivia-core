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

That is the rule for every nested failure this module answers, not only a provider's.
The contract validators reached from `_validate_descriptor`, and the calendar check in
`_timestamp`, are caught the same way and answered the same way: a sentinel set inside
the handler, the refusal raised once it has exited. Three of them used `from None`, and
two of those said in their own words -- `_endpoint` in its docstring, `_version_window`
in an inline comment -- that this kept the nested text off the caller's error. It did
not, by the argument two paragraphs up, and a comment claiming a security property the
code does not have is worse than no comment at all.

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
from `ServiceComponentStatus.id` because an `Identifier` cannot contain one, and a
workspace database is refused from `endpoint_uri` because `ServiceEndpointUri`
admits only an approved dialable transport, which a database is not.

That last field is the one published value whose policy this module does not own.
P0 froze `endpoint_uri` as the coordination fact a client needs to dial the
instance at all, and froze *which* endpoints may be published as one: Core's
generated `ServiceEndpointUri` pattern is the single authority, compiled directly
by every binding so that no runtime adds acceptance rules of its own. So the
endpoint URI is handed to `validate_service_endpoint_uri` rather than assessed
again here. The permissive rule that used to sit in its place was a second policy
in all but name, and it disagreed: it published `file:///.../workspace.sqlite`, a
credential-bearing query and an unapproved scheme, because each of those is a
well-formed absolute URI carrying nothing in the `userinfo` position it looked at.

Only the *refusal* stays local. What an unauthenticated caller may be told is this
boundary's call rather than something inherited from a library raising to its own
caller, so Core's `ContractSemanticError` is translated into a fixed `ProbeError`
that names the field and stops.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeAlias, TypeVar

from omnivia_core.contracts.v1 import (
    CapabilityRef,
    ContractSemanticError,
    ServiceComponentStatus,
    ServiceEndpointDescriptor,
    ServiceProbeRequest,
    ServiceProbeResult,
    ServiceProcessEvidence,
    VersionWindow,
    duplicate_capability_ids,
    is_capability_id,
    is_contract_version,
    is_identifier,
    is_open_code,
    is_probe_status,
    is_release_version,
    is_request_id,
    is_timestamp,
    is_workspace_id,
    validate_service_endpoint_uri,
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
# A schema-declared scalar restates nothing here: its grammar *and* its length
# bounds are both applied by the contract's own generated guard, which the
# generator emits from the declaration. A bound this module cannot restate is a
# bound this module cannot get wrong, and the four it used to restate by hand are
# gone with the `(pattern, minimum, maximum)` table that held them.
#
# What is left below is bounds the generated guards do not carry: an array's
# `maxItems`, an integer's `maximum`, and the one published string the schema
# bounds without patterning.

#: `maxLength` of the 128-bounded scalars, applied only to the two values with no
#: generated guard of their own: `ServiceProcessEvidence.start_time`, and the probe
#: kind on an unvalidated inbound request.
_BOUNDED_MAX: Final = 128

#: `maxItems` on both published arrays -- `components` and
#: `supported_capabilities`. A snapshot naming more subsystems than the contract
#: publishes is refused rather than truncated: a silently shortened list reads as a
#: complete one, and a caller deciding whether to route traffic here would be
#: deciding on a subset nothing told it was a subset.
_MAX_PUBLISHED_ITEMS: Final = 256

#: `DurationMs.maximum`, in milliseconds: twenty-four hours.
_DURATION_MS_MAX: Final = 86_400_000

#: One value domain: a predicate that is true of the values it admits. The
#: schema-declared domains are the contract's own generated guards -- `is_identifier`,
#: `is_timestamp` and the rest -- called rather than re-implemented, so this module
#: cannot hold a published field to a grammar or a bound the contract does not
#: declare. That is what this module used to do: a local `(pattern, minimum,
#: maximum)` table restating bounds the schema already states, which is a third
#: independent copy of "apply the declared value domain" and the copy most likely to
#: fall behind, because nothing failed when it did.
_Guard: TypeAlias = Callable[[object], bool]

#: `ServiceProcessEvidence.start_time` is the one published string the schema
#: bounds without patterning: its spelling is whatever the host platform reports,
#: so there is no grammar to hold it to, and no generated guard exists for it.
#: Control characters are still refused -- a value carrying a newline or an ANSI
#: escape is not a platform reading, it is something that will be pasted into an
#: operator's terminal.
_OPAQUE_TEXT_RE: Final = re.compile(r"[^\x00-\x1f\x7f-\x9f]+")


def _is_opaque_text(value: object) -> bool:
    """The one domain declared by no schema pattern, in the generated guards' shape."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _BOUNDED_MAX
        and _OPAQUE_TEXT_RE.fullmatch(value) is not None
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


def _scalar(value: object, label: str, guard: _Guard) -> str:
    """Return `value` when it is a publishable instance of `guard`'s domain, else refuse.

    The type is narrowed before `guard` runs, so a non-string is refused as one
    rather than as a malformed string. Everything after that -- the declared length
    bounds, the declared grammar, and for a declared `format: date-time` the calendar
    -- is the guard's, applied in the guard's order, which is the same order in the
    contract's other binding because one loop over the schema emits both.

    A message names the field and stops. Not the value, not its length, not the
    grammar it failed: a probe answers before authentication, so the whole of what
    this can safely say is *which* field a caller's snapshot got wrong. That is why
    the refusal is one fixed sentence per field and not the guard's own verdict.
    """
    if not isinstance(value, str):
        raise ProbeError(f"{label} is not a string")
    if not guard(value):
        raise ProbeError(f"{label} is malformed")
    return value


def _timestamp(value: object, label: str) -> None:
    """Refuse anything that is not a real instant spelled as a `Timestamp`.

    A pattern is not a calendar. `2026-13-45T99:99:99Z` satisfies
    `TIMESTAMP_PATTERN` character for character and names no moment that has ever
    existed. Both halves now live in the generated `is_timestamp`, which the
    contract's TypeScript binding mirrors clause for clause, so this boundary and a
    TypeScript caller refuse the same instants -- they did not before, and nothing
    here could have told you so.

    Parsing is not clock-reading. This reads a value the caller handed over; the
    router still owns no clock, and the only time it knows is the monotonic one
    injected for deadlines.

    Kept as its own name rather than inlined at the eight call sites: it is the
    field-labelled refusal, and `_scalar` is where the label is applied.
    """
    _scalar(value, label, is_timestamp)


def _endpoint(descriptor: ServiceEndpointDescriptor, label: str) -> None:
    """Refuse a descriptor whose endpoint Core's accepted policy will not publish.

    The one published field this module does not judge for itself. `endpoint_uri` is
    also the one that is *meant* to carry a path -- a unix socket lives somewhere on
    a filesystem and a client cannot dial it without being told where -- so "does
    this look like a path" was never the question, and the question it is instead is
    settled by Core: the generated `ServiceEndpointUri` pattern decides which
    endpoints publish, and every binding compiles that one pattern so no runtime
    acquires acceptance rules of its own.

    Which is why nothing is restated here. The permissive rule this replaced asked
    only whether the value was an absolute URI with nothing in the `userinfo`
    position, and `file:///Users/.../workspace.sqlite`, an unapproved scheme and
    `http://host/?access_token=...` all answered yes -- a second, laxer policy that
    read like the same one.

    The refusal is local and fixed. Core names no value in its own message today
    either, but a probe answers before authentication, so what that caller is told
    is decided at this boundary rather than inherited from a library raising to its
    caller, and it stays decided here whatever Core's wording becomes.

    Which is why the contract's error is dropped rather than chained, and why that
    is done by raising after the handler has exited. `raise ... from None` would
    read as the same thing and is not: it clears `__cause__` and sets
    `__suppress_context__`, so a rendered traceback stays quiet while the
    contract's exception -- and the frames inside the validator that produced it --
    remain on `__context__`, one attribute access away for anything that logs or
    serializes the error a caller catches.

    Core is asked about the endpoint URI, not about the descriptor. This label names
    one field, so it may only ever answer for one field. Asking
    `validate_service_endpoint_descriptor` was indistinguishable from asking about
    the URI while that validator checked `endpoint_uri` and nothing else, and became
    a wrong answer the moment it also checked `published_at`: a malformed timestamp
    was reported to an unauthenticated caller as an unapproved transport endpoint,
    naming the one field that was fine. Every other published field is held to its
    own grammar by :func:`_validate_descriptor` and reports under its own name, so
    the narrow question is also the only one whose answer this label can carry --
    and it stays correct as Core enforces more of the descriptor's value domains.
    """
    approved = True
    try:
        validate_service_endpoint_uri(descriptor.endpoint_uri)
    except ContractSemanticError:
        approved = False
    if not approved:
        raise ProbeError(f"{label} is not an approved transport endpoint")


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
    _scalar(window.minimum, f"{label} minimum", is_contract_version)
    _scalar(window.maximum, f"{label} maximum", is_contract_version)
    # Refused with a message of this module's own, and the contract's is not
    # carried along either. Its text quotes the bounds it rejected -- `version
    # window is reversed: minimum '9.9' exceeds maximum '1.0'` -- which is right
    # for a library raising to its caller and wrong here: nothing nested is vetted
    # for what an unauthenticated caller may be told, so nothing nested is passed
    # on. The sentinel is what makes that true. `from None` would clear
    # `__cause__` and silence the rendering of `__context__` while leaving the
    # quoted bounds reachable on the error a caller catches; raising once the
    # handler has exited means there is no exception being handled to chain to.
    ordered = True
    try:
        validate_version_window(window)
    except ContractSemanticError:
        ordered = False
    if not ordered:
        raise ProbeError(f"{label} is malformed")


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
        _scalar(request.request_id, "probe request request_id", is_request_id)

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
        _scalar(status, f"service facts {label}", is_probe_status)

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
        _scalar(component.id, f"{where} id", is_identifier)
        _scalar(component.status, f"{where} status", is_probe_status)
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

    Every field but one is held to its own public grammar here. `endpoint_uri` is
    held to Core's, whose generated pattern is the policy rather than a description
    of it -- see :func:`_endpoint`.
    """
    label = "service facts descriptor"
    _scalar(
        descriptor.descriptor_version, f"{label} descriptor_version", is_contract_version
    )
    _scalar(descriptor.workspace_id, f"{label} workspace_id", is_workspace_id)
    _scalar(descriptor.service_instance_id, f"{label} service_instance_id", is_identifier)
    _scalar(descriptor.installation_id, f"{label} installation_id", is_identifier)
    _endpoint(descriptor, f"{label} endpoint_uri")
    _scalar(descriptor.protocol_version, f"{label} protocol_version", is_contract_version)
    _scalar(descriptor.server_version, f"{label} server_version", is_release_version)
    _version_window(
        descriptor.supported_api_versions, f"{label} supported_api_versions"
    )
    _version_window(
        descriptor.supported_workspace_versions, f"{label} supported_workspace_versions"
    )
    _scalar(
        descriptor.workspace_format_version,
        f"{label} workspace_format_version",
        is_contract_version,
    )
    if not isinstance(descriptor.ready, bool):
        raise ProbeError(f"{label} ready is not a boolean")
    _scalar(descriptor.lifecycle_state, f"{label} lifecycle_state", is_open_code)
    _bounded_int(descriptor.fencing_generation, f"{label} fencing_generation", 1)
    _timestamp(descriptor.published_at, f"{label} published_at")

    process = descriptor.process
    if process is None:
        return
    if not isinstance(process, ServiceProcessEvidence):
        raise ProbeError(f"{label} process is not process evidence")
    _bounded_int(process.pid, f"{label} process pid", 1)
    _scalar(process.start_time, f"{label} process start_time", _is_opaque_text)
    _scalar(process.boot_id, f"{label} process boot_id", is_identifier)


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
        _scalar(ref.id, f"supported capability {index} id", is_capability_id)
        _scalar(ref.version, f"supported capability {index} version", is_contract_version)
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
