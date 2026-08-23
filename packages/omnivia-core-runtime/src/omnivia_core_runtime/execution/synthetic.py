"""Runtime Execution Planes: the synthetic external-system conformance oracle.

RXP-GATE-D, first bounded half. This is a deterministic, in-memory,
non-authoritative oracle over five synthetic external systems -- CRM, email,
accounting, storage and webhook -- proving effect uncertainty, retry and
duplicate controls the way :mod:`planes`, :mod:`registry` and :mod:`workflow`
already prove their own seams. It holds no database, scheduler, transport,
credential or Platform handle, dispatches nothing over a network, and writes
no canonical state. Browser/network/session adversarial fixtures are the next
packet's concern, not this one's.

Every dispatch is addressed by a stable logical effect identity --
:attr:`EffectIntent.effect_key`, derived from ``(system, operation,
external_id)`` -- so a duplicate dispatch of the same intent never creates a
second logical external effect: a completed one replays, and an uncertain one
is refused rather than repeated. :data:`CRASH_POINTS` names where a dispatch
can crash: before the intent is recorded, after it, during the provider call,
after the provider commits, and before the local receipt is appended. Each
crash point resolves to one of four reconciliation states --
:data:`RECONCILE_APPLIED`, :data:`RECONCILE_NOT_APPLIED`,
:data:`RECONCILE_PARTIAL`, :data:`RECONCILE_UNKNOWN` -- and only
``NOT_APPLIED`` is a retryable state: it is the one state this oracle treats
as an explicit absence proof. ``PARTIAL`` and ``UNKNOWN`` fail closed and can
only be moved by :meth:`SyntheticExternalOracle.reconcile`, never by a bare
retry.

:meth:`SyntheticExternalOracle.reconcile` and
:meth:`SyntheticExternalOracle.receive_webhook` both resolve the *current*
attempt only. A reconcile named against an attempt this oracle has already
superseded is retained as audit evidence and never overwrites the current
attempt's result; a webhook delivered twice under the same external event id
resolves once and replays its cached result on every later delivery, so a
duplicate webhook is exactly as inert as a duplicate dispatch.

RXP-GATE-D, second bounded half. :class:`SyntheticBrowserSessionOracle` and
:class:`SyntheticNetworkPolicy` extend the same seam to browser session
lifecycle and network egress, sharing nothing with the five effect systems
above beyond the vocabulary helpers in :mod:`profile`. A browser session is a
single-owner, positively-fenced lease that is either disposed or reported as
leaked, never silently active. A network request is judged against a closed
set of egress controls rather than any live DNS lookup, browser process or
transport. Both remain deterministic, in-memory and non-authoritative, and
neither ever stores the raw contents of a denied request -- only the host,
kind and reason a caller can act on -- so a prompt-injection fixture that
tries to exfiltrate a secret through a denied navigation leaves a denial
record behind and never the secret itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Final

from omnivia_core_runtime.execution.profile import (
    ExecutionContractError,
    ExecutionRefused,
    derive_id,
    require_collection,
    require_digest,
    require_identifier,
    require_vocabulary,
)

SYSTEM_CRM: Final = "CRM"
SYSTEM_EMAIL: Final = "EMAIL"
SYSTEM_ACCOUNTING: Final = "ACCOUNTING"
SYSTEM_STORAGE: Final = "STORAGE"
SYSTEM_WEBHOOK: Final = "WEBHOOK"

SYSTEMS: Final[frozenset[str]] = frozenset(
    {
        SYSTEM_CRM,
        SYSTEM_EMAIL,
        SYSTEM_ACCOUNTING,
        SYSTEM_STORAGE,
        SYSTEM_WEBHOOK,
    }
)

#: Where a dispatch can crash. ``NONE`` is the successful case and shares this
#: vocabulary rather than living outside it, because :meth:`SyntheticExternalOracle.dispatch`
#: takes exactly one member of one vocabulary to say what happened.
CRASH_NONE: Final = "NONE"
CRASH_BEFORE_INTENT: Final = "BEFORE_INTENT"
CRASH_AFTER_INTENT: Final = "AFTER_INTENT"
CRASH_DURING_DISPATCH: Final = "DURING_DISPATCH"
CRASH_AFTER_PROVIDER_COMMIT: Final = "AFTER_PROVIDER_COMMIT"
CRASH_BEFORE_RECEIPT_APPEND: Final = "BEFORE_RECEIPT_APPEND"

CRASH_POINTS: Final[frozenset[str]] = frozenset(
    {
        CRASH_NONE,
        CRASH_BEFORE_INTENT,
        CRASH_AFTER_INTENT,
        CRASH_DURING_DISPATCH,
        CRASH_AFTER_PROVIDER_COMMIT,
        CRASH_BEFORE_RECEIPT_APPEND,
    }
)

#: The four reconciliation outcomes. ``APPLIED`` and ``NOT_APPLIED`` are the
#: two an absence or presence proof can settle on; ``PARTIAL`` and ``UNKNOWN``
#: are the two this oracle never resolves on its own -- only an explicit
#: reconcile can move an effect out of either.
RECONCILE_APPLIED: Final = "APPLIED"
RECONCILE_NOT_APPLIED: Final = "NOT_APPLIED"
RECONCILE_PARTIAL: Final = "PARTIAL"
RECONCILE_UNKNOWN: Final = "UNKNOWN"

RECONCILE_STATES: Final[frozenset[str]] = frozenset(
    {
        RECONCILE_APPLIED,
        RECONCILE_NOT_APPLIED,
        RECONCILE_PARTIAL,
        RECONCILE_UNKNOWN,
    }
)

#: The states an external proof may assert. ``UNKNOWN`` is never asserted --
#: it is the state before any proof arrives, not a claim a proof can make.
ASSERTABLE_RECONCILE_STATES: Final[frozenset[str]] = frozenset(
    {RECONCILE_APPLIED, RECONCILE_NOT_APPLIED, RECONCILE_PARTIAL}
)

#: Retry is only ever safe from this one state: an explicit absence proof.
RETRYABLE_STATES: Final[frozenset[str]] = frozenset({RECONCILE_NOT_APPLIED})


@dataclass(frozen=True, slots=True)
class EffectIntent:
    """One caller's declared intent to cause one effect against one system.

    Identity is business-level, not attempt-level: two intents naming the same
    ``(system, operation, external_id)`` name the same logical effect, so a
    resend of the same intent is a duplicate dispatch rather than a second
    effect.
    """

    system: str
    operation: str
    external_id: str

    def __post_init__(self) -> None:
        require_vocabulary("system", self.system, SYSTEMS)
        require_identifier("operation", self.operation)
        require_identifier("external_id", self.external_id)

    @property
    def effect_key(self) -> str:
        """The stable logical effect identity this intent dispatches against."""
        return derive_id("synthetic_effect", self.system, self.operation, self.external_id)


@dataclass(frozen=True, slots=True)
class EffectRecord:
    """One effect's current (or once-current) attempt: what happened, and how."""

    effect_key: str
    system: str
    operation: str
    external_id: str
    attempt: int
    state: str
    crash_point: str
    receipt_ref: str | None = None

    def __post_init__(self) -> None:
        require_digest("effect_key", self.effect_key)
        require_vocabulary("system", self.system, SYSTEMS)
        require_identifier("operation", self.operation)
        require_identifier("external_id", self.external_id)
        if self.attempt < 1:
            raise ExecutionContractError("invalid_attempt", "attempt must be positive")
        require_vocabulary("state", self.state, RECONCILE_STATES)
        require_vocabulary("crash_point", self.crash_point, CRASH_POINTS)
        if self.receipt_ref is not None:
            require_digest("receipt_ref", self.receipt_ref)


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """One inbound webhook delivery: a provider's claim about one effect.

    ``external_event_id`` is the provider's own delivery identity. Two
    deliveries carrying the same id are the same delivery, redelivered -- the
    oracle resolves the first and replays that result for every one after.
    """

    system: str
    external_event_id: str
    effect_key: str
    outcome: str
    receipt_ref: str | None = None

    def __post_init__(self) -> None:
        require_vocabulary("system", self.system, SYSTEMS)
        require_identifier("external_event_id", self.external_event_id)
        require_digest("effect_key", self.effect_key)
        require_vocabulary("outcome", self.outcome, ASSERTABLE_RECONCILE_STATES)
        if self.receipt_ref is not None:
            require_digest("receipt_ref", self.receipt_ref)

    @property
    def event_key(self) -> str:
        """The stable identity of this delivery, for webhook-level dedup."""
        return derive_id("synthetic_webhook_event", self.system, self.external_event_id)


def _simulated_outcome(crash_point: str, effect_key: str, attempt: int) -> tuple[str, str | None]:
    """Return the ``(state, receipt_ref)`` one dispatch attempt resolves to.

    ``CRASH_BEFORE_INTENT`` is handled by the caller before this is reached --
    nothing is recorded for it, so it has no outcome to simulate here.
    """
    if crash_point == CRASH_NONE:
        return RECONCILE_APPLIED, derive_id("synthetic_receipt", effect_key, str(attempt))
    if crash_point == CRASH_AFTER_INTENT:
        # The intent was durably recorded but the provider was never called --
        # an absence this oracle itself witnessed, not merely suspects.
        return RECONCILE_NOT_APPLIED, None
    if crash_point == CRASH_DURING_DISPATCH:
        # The provider call was in flight when the crash happened: neither a
        # presence nor an absence proof exists yet.
        return RECONCILE_UNKNOWN, None
    if crash_point == CRASH_AFTER_PROVIDER_COMMIT:
        # The provider committed the effect but no local evidence of that
        # commit survived the crash.
        return RECONCILE_PARTIAL, None
    # CRASH_BEFORE_RECEIPT_APPEND: the receipt was generated but never
    # durably appended -- partial evidence exists, but the effect record
    # itself is not yet complete.
    return RECONCILE_PARTIAL, derive_id("synthetic_receipt", effect_key, str(attempt))


class SyntheticExternalOracle:
    """A deterministic, in-memory oracle over five synthetic external systems.

    One instance covers all five systems: the dedup, retry and reconciliation
    rules are the same shape regardless of which system an effect targets, so
    five oracles would be one rule enforced five times.
    """

    def __init__(self) -> None:
        self._current: dict[str, EffectRecord] = {}
        self._history: dict[str, dict[int, EffectRecord]] = {}
        self._late_evidence: dict[str, list[EffectRecord]] = {}
        self._webhook_events: dict[str, EffectRecord] = {}

    def current(self, effect_key: str) -> EffectRecord | None:
        """Return the current attempt's record for ``effect_key``, if any."""
        return self._current.get(effect_key)

    def history(self, effect_key: str) -> tuple[EffectRecord, ...]:
        """Return every attempt ever dispatched for ``effect_key``, in order."""
        attempts = self._history.get(effect_key, {})
        return tuple(attempts[attempt] for attempt in sorted(attempts))

    def audit_trail(self, effect_key: str) -> tuple[EffectRecord, ...]:
        """Return every late, superseded-attempt confirmation retained for ``effect_key``."""
        return tuple(self._late_evidence.get(effect_key, []))

    def dispatch(self, intent: EffectIntent, *, crash_point: str = CRASH_NONE) -> EffectRecord:
        """Dispatch ``intent``, or replay/refuse a duplicate under the same effect key.

        A completed effect replays its result rather than dispatching again. An
        effect left ``UNKNOWN`` or ``PARTIAL`` fails closed: it must be resolved
        by :meth:`reconcile` before another attempt is admitted. Only an effect
        that has never been attempted, or one explicitly proven
        ``NOT_APPLIED``, may proceed to a new attempt.
        """
        require_vocabulary("crash_point", crash_point, CRASH_POINTS)
        key = intent.effect_key
        if crash_point == CRASH_BEFORE_INTENT:
            raise ExecutionRefused(
                "crash_before_intent", "no intent was recorded before the crash"
            )
        existing = self._current.get(key)
        if existing is not None:
            if existing.state == RECONCILE_APPLIED:
                return existing
            if existing.state not in RETRYABLE_STATES:
                raise ExecutionRefused(
                    "retry_blocked",
                    f"effect is {existing.state}; retry requires an explicit "
                    "absence proof via reconcile",
                )
        attempt = 1 if existing is None else existing.attempt + 1
        state, receipt_ref = _simulated_outcome(crash_point, key, attempt)
        record = EffectRecord(
            key,
            intent.system,
            intent.operation,
            intent.external_id,
            attempt,
            state,
            crash_point,
            receipt_ref,
        )
        self._current[key] = record
        self._history.setdefault(key, {})[attempt] = record
        return record

    def reconcile(
        self,
        effect_key: str,
        attempt: int,
        outcome: str,
        *,
        receipt_ref: str | None = None,
    ) -> EffectRecord:
        """Resolve one attempt against an external proof, or refuse it.

        A proof named against the current attempt settles that attempt --
        redundant confirmation of an already-terminal state is accepted only
        if it agrees, and a contradicting one is refused. A proof named
        against a superseded attempt is retained in :meth:`audit_trail` and
        never touches the current attempt's result. A proof naming an attempt
        this effect has not reached yet is refused outright.
        """
        require_digest("effect_key", effect_key)
        if attempt < 1:
            raise ExecutionContractError("invalid_attempt", "attempt must be positive")
        require_vocabulary("outcome", outcome, ASSERTABLE_RECONCILE_STATES)
        if receipt_ref is not None:
            require_digest("receipt_ref", receipt_ref)
        current = self._current.get(effect_key)
        if current is None:
            raise ExecutionRefused("unknown_effect", f"no effect known as {effect_key!r}")
        if attempt > current.attempt:
            raise ExecutionRefused(
                "unknown_attempt", "proof names an attempt this effect has not reached"
            )
        attempted = self._history[effect_key][attempt]
        if attempt < current.attempt:
            late = replace(attempted, state=outcome, receipt_ref=receipt_ref)
            self._late_evidence.setdefault(effect_key, []).append(late)
            return current
        if current.state in (RECONCILE_APPLIED, RECONCILE_NOT_APPLIED):
            if outcome != current.state:
                raise ExecutionRefused(
                    "reconcile_conflict",
                    "proof contradicts this attempt's already-resolved outcome",
                )
            return current
        resolved = replace(
            current,
            state=outcome,
            receipt_ref=receipt_ref if receipt_ref is not None else current.receipt_ref,
        )
        self._current[effect_key] = resolved
        self._history[effect_key][attempt] = resolved
        return resolved

    def receive_webhook(self, event: WebhookEvent) -> EffectRecord:
        """Process one webhook delivery, replaying a duplicate delivery's result.

        Dedup is by :attr:`WebhookEvent.event_key`, the provider's own
        delivery identity: a redelivered event resolves nothing a second time
        and simply returns the result the first delivery produced.
        """
        cached = self._webhook_events.get(event.event_key)
        if cached is not None:
            return cached
        current = self._current.get(event.effect_key)
        if current is None:
            raise ExecutionRefused(
                "unknown_effect", f"no effect known as {event.effect_key!r}"
            )
        result = self.reconcile(
            event.effect_key,
            current.attempt,
            event.outcome,
            receipt_ref=event.receipt_ref,
        )
        self._webhook_events[event.event_key] = result
        return result


# --------------------------------------------------------------------------
# Browser session lifecycle
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrowserSession:
    """A single-owner, positively-fenced synthetic browser session lease."""

    session_id: str
    owner: str
    fence: int
    disposed: bool = False

    def __post_init__(self) -> None:
        require_identifier("session_id", self.session_id)
        require_identifier("owner", self.owner)
        if self.fence < 1:
            raise ExecutionContractError("invalid_fence", "fence must be a positive integer")


class SyntheticBrowserSessionOracle:
    """A deterministic, in-memory oracle over synthetic browser session leases.

    Exactly one owner may hold a session at a time, under exactly one positive
    fencing token. Every action against a held session -- including release --
    must present the owner and fence it is currently held under, so a stale
    caller can never act as though it still held a session that has moved on.
    Release is idempotent for the owner/fence that performed it: releasing an
    already-disposed session under the same owner/fence replays that
    disposal rather than refusing it. :meth:`leaked` names every session ever
    acquired that is not, right now, disposed -- the one bounded check a
    cancellation or restart simulation is judged against.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, BrowserSession] = {}
        self._acquired: set[str] = set()

    def acquire(self, session_id: str, owner: str) -> BrowserSession:
        """Take ``session_id`` exclusively for ``owner``, refusing while another owner holds it."""
        require_identifier("session_id", session_id)
        require_identifier("owner", owner)
        current = self._sessions.get(session_id)
        if current is not None and not current.disposed:
            raise ExecutionRefused(
                "session_held", f"session {session_id!r} is already held"
            )
        fence = 1 if current is None else current.fence + 1
        session = BrowserSession(session_id, owner, fence)
        self._sessions[session_id] = session
        self._acquired.add(session_id)
        return session

    def active(self, session_id: str, owner: str, fence: int) -> BrowserSession:
        """Return the session if ``owner``/``fence`` currently holds it, else refuse."""
        current = self._sessions.get(session_id)
        if current is None or current.disposed:
            raise ExecutionRefused(
                "no_active_session", f"session {session_id!r} is not held"
            )
        if current.owner != owner or current.fence != fence:
            raise ExecutionRefused(
                "stale_owner_or_fence", "action names a superseded owner or fence"
            )
        return current

    def release(self, session_id: str, owner: str, fence: int) -> BrowserSession:
        """Dispose ``session_id``. Idempotent for the owner/fence that disposed it."""
        current = self._sessions.get(session_id)
        if (
            current is not None
            and current.disposed
            and current.owner == owner
            and current.fence == fence
        ):
            return current
        active = self.active(session_id, owner, fence)
        disposed = replace(active, disposed=True)
        self._sessions[session_id] = disposed
        return disposed

    def leaked(self) -> tuple[str, ...]:
        """Every session id ever acquired that is not, right now, disposed."""
        return tuple(
            session_id
            for session_id in sorted(self._acquired)
            if not self._sessions[session_id].disposed
        )


# --------------------------------------------------------------------------
# Network egress policy
# --------------------------------------------------------------------------

NETWORK_ALLOW: Final = "ALLOW"
NETWORK_DENY: Final = "DENY"

NETWORK_DECISIONS: Final[frozenset[str]] = frozenset({NETWORK_ALLOW, NETWORK_DENY})

NETWORK_KIND_NAVIGATION: Final = "NAVIGATION"
NETWORK_KIND_REDIRECT: Final = "REDIRECT"
NETWORK_KIND_WEBSOCKET: Final = "WEBSOCKET"
NETWORK_KIND_DOWNLOAD: Final = "DOWNLOAD"
NETWORK_KIND_CHILD_PROCESS_EGRESS: Final = "CHILD_PROCESS_EGRESS"
NETWORK_KIND_PROXY_BYPASS: Final = "PROXY_BYPASS"

NETWORK_KINDS: Final[frozenset[str]] = frozenset(
    {
        NETWORK_KIND_NAVIGATION,
        NETWORK_KIND_REDIRECT,
        NETWORK_KIND_WEBSOCKET,
        NETWORK_KIND_DOWNLOAD,
        NETWORK_KIND_CHILD_PROCESS_EGRESS,
        NETWORK_KIND_PROXY_BYPASS,
    }
)

#: Kinds that are never navigable, regardless of host or scheme -- a browser
#: session that can spawn a child process, open a raw socket, trigger a
#: download or route around the policy has already left the seam a host
#: allowlist can reason about.
_ALWAYS_DENIED_KINDS: Final[frozenset[str]] = frozenset(
    {
        NETWORK_KIND_WEBSOCKET,
        NETWORK_KIND_DOWNLOAD,
        NETWORK_KIND_CHILD_PROCESS_EGRESS,
        NETWORK_KIND_PROXY_BYPASS,
    }
)

_MAX_URL_LENGTH: Final = 2048
_UNKNOWN_HOST: Final = "-"

#: A deliberately small URL grammar: scheme, optional userinfo, host (bare,
#: dotted, or a bracketed IPv6 literal), optional port, and whatever follows.
#: This seam's import allowlist admits no URL library, so parsing is done
#: locally against exactly the fields a navigation decision needs.
_URL_RE: Final = re.compile(
    r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://"
    r"(?:[^@/?#]*@)?"
    r"(?P<host>\[[0-9a-fA-F:]*\]|[^:/?#]*)"
    r"(?::\d+)?"
    r"(?:[/?#].*)?$"
)


def _parse_url(url: str) -> tuple[str, str | None]:
    """Return ``(scheme, host)`` for ``url``, lowercased, or ``(scheme, None)``."""
    match = _URL_RE.match(url)
    if match is None:
        return "", None
    scheme = match.group("scheme").lower()
    host = match.group("host") or ""
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return scheme, host.lower() if host else None


def _parse_ipv4(host: str) -> tuple[int, int, int, int] | None:
    """Return the four octets of ``host`` as an IPv4 literal, or ``None``."""
    parts = host.split(".")
    if len(parts) != 4:
        return None
    octets: list[int] = []
    for part in parts:
        if not part.isdigit() or not 1 <= len(part) <= 3:
            return None
        if len(part) > 1 and part[0] == "0":
            return None
        value = int(part)
        if value > 255:
            return None
        octets.append(value)
    return octets[0], octets[1], octets[2], octets[3]


# ponytail: covers the address ranges the required fixtures name (IPv4
# loopback/private/link-local, and IPv6 loopback/unique-local/link-local by
# prefix) rather than the full ipaddress module's range tables. Upgrade to
# the stdlib `ipaddress` module if this seam's import allowlist ever widens
# to admit it.
def _disallowed_host_reason(host: str) -> str | None:
    """Return why ``host`` is never a valid navigation target, or ``None``.

    A domain must be used, never a raw address: any address literal -- loopback,
    link-local (which is where the ``169.254.169.254`` cloud metadata endpoint
    lives), private or otherwise -- is refused, so this never depends on a live
    DNS lookup to tell a public address from a private one.
    """
    if host == "localhost" or host.endswith(".localhost"):
        return "loopback_host"
    ipv4 = _parse_ipv4(host)
    if ipv4 is not None:
        first, second, _third, _fourth = ipv4
        if first == 127:
            return "loopback_address"
        if first == 169 and second == 254:
            return "link_local_address"
        if (
            first == 10
            or (first == 172 and 16 <= second <= 31)
            or (first == 192 and second == 168)
        ):
            return "private_address"
        return "direct_ip_destination"
    if ":" in host:
        if host == "::1":
            return "loopback_address"
        if host.startswith(("fe8", "fe9", "fea", "feb")):
            return "link_local_address"
        if host.startswith(("fc", "fd")):
            return "private_address"
        return "direct_ip_destination"
    return None


@dataclass(frozen=True, slots=True)
class NetworkRequest:
    """One synthetic browser network request, judged without any real transport.

    ``source_origin`` is the origin the caller *claims* the session is already
    on. It is never trusted on its own: same-origin navigation is admitted
    only when it also matches one of the policy's own
    :attr:`SyntheticNetworkPolicy` trusted current origins, so a caller cannot
    self-declare an attacker origin into the allowlist by simply asserting it
    as the source. A redirect to a third origin still needs that origin to be
    independently allowlisted.
    """

    kind: str
    url: str
    source_origin: str | None = None

    def __post_init__(self) -> None:
        require_vocabulary("kind", self.kind, NETWORK_KINDS)
        if not isinstance(self.url, str) or not self.url or len(self.url) > _MAX_URL_LENGTH:
            raise ExecutionContractError(
                "invalid_url", "url must be a non-empty bounded string"
            )
        if self.source_origin is not None:
            require_identifier("source_origin", self.source_origin)

    @property
    def host(self) -> str | None:
        """The lowercase hostname of :attr:`url`, or ``None`` if it cannot be parsed."""
        return _parse_url(self.url)[1]

    @property
    def scheme(self) -> str:
        return _parse_url(self.url)[0]


@dataclass(frozen=True, slots=True)
class NetworkDecision:
    """What the policy decided about one request, and why -- never the request itself.

    Carrying the host and reason but never the url means a denial is retained
    as evidence without retaining anything a url's path or query string might
    have carried, including a prompt-injection fixture's attempted secret.
    """

    decision: str
    kind: str
    host: str
    reason: str

    def __post_init__(self) -> None:
        require_vocabulary("decision", self.decision, NETWORK_DECISIONS)
        require_vocabulary("kind", self.kind, NETWORK_KINDS)
        require_identifier("reason", self.reason)


class SyntheticNetworkPolicy:
    """A deterministic, in-memory allowlist over synthetic browser network egress.

    Only an explicitly allowed HTTPS origin, or same-origin navigation against
    a policy-owned trusted current origin, is admitted. Same-origin admission
    is never decided from the request alone -- :attr:`NetworkRequest.source_origin`
    is a caller assertion, and a caller asserting an untrusted origin as its
    own source must not be able to navigate to that origin. Only a source that
    matches one of this policy's own ``trusted_current_https_origins`` --
    validated at construction under the same host restrictions as the
    allowlist -- can admit same-origin navigation. Every denial -- including
    one purpose-built to imitate a prompt-injection attempt at secret
    exfiltration or navigation to an attacker origin -- is retained in
    :attr:`denials` as host/kind/reason evidence only.
    """

    def __init__(
        self,
        allowed_https_origins: tuple[str, ...] | frozenset[str],
        trusted_current_https_origins: tuple[str, ...] | frozenset[str] = (),
    ) -> None:
        origins = require_collection(
            "allowed_https_origins", tuple(allowed_https_origins), required=True
        )
        self._allowed = frozenset(origin.lower() for origin in origins)
        trusted = require_collection(
            "trusted_current_https_origins",
            tuple(trusted_current_https_origins),
            required=False,
        )
        self._trusted = frozenset(origin.lower() for origin in trusted)
        for origin in self._trusted:
            reason = _disallowed_host_reason(origin)
            if reason is not None:
                raise ExecutionContractError(
                    "invalid_trusted_origin",
                    f"trusted_current_https_origins entry {origin!r} is {reason}",
                )
        self._denials: list[NetworkDecision] = []

    @property
    def denials(self) -> tuple[NetworkDecision, ...]:
        """Every denial decision made so far, in the order it was made."""
        return tuple(self._denials)

    def evaluate(self, request: NetworkRequest) -> NetworkDecision:
        """Decide ``request``, recording it in :attr:`denials` if it is denied."""
        decision = self._decide(request)
        if decision.decision == NETWORK_DENY:
            self._denials.append(decision)
        return decision

    def _decide(self, request: NetworkRequest) -> NetworkDecision:
        if request.kind in _ALWAYS_DENIED_KINDS:
            return NetworkDecision(NETWORK_DENY, request.kind, _UNKNOWN_HOST, "kind_not_navigable")
        host = request.host
        if host is None:
            return NetworkDecision(NETWORK_DENY, request.kind, _UNKNOWN_HOST, "unparseable_url")
        if request.scheme != "https":
            return NetworkDecision(NETWORK_DENY, request.kind, host, "non_https_scheme")
        disallowed = _disallowed_host_reason(host)
        if disallowed is not None:
            return NetworkDecision(NETWORK_DENY, request.kind, host, disallowed)
        if host in self._allowed:
            return NetworkDecision(NETWORK_ALLOW, request.kind, host, "allowlisted_origin")
        if (
            request.source_origin is not None
            and request.source_origin.lower() == host
            and host in self._trusted
        ):
            return NetworkDecision(
                NETWORK_ALLOW, request.kind, host, "same_origin_navigation"
            )
        return NetworkDecision(NETWORK_DENY, request.kind, host, "origin_not_allowlisted")


__all__ = [
    "ASSERTABLE_RECONCILE_STATES",
    "CRASH_AFTER_INTENT",
    "CRASH_AFTER_PROVIDER_COMMIT",
    "CRASH_BEFORE_INTENT",
    "CRASH_BEFORE_RECEIPT_APPEND",
    "CRASH_DURING_DISPATCH",
    "CRASH_NONE",
    "CRASH_POINTS",
    "NETWORK_ALLOW",
    "NETWORK_DECISIONS",
    "NETWORK_DENY",
    "NETWORK_KINDS",
    "NETWORK_KIND_CHILD_PROCESS_EGRESS",
    "NETWORK_KIND_DOWNLOAD",
    "NETWORK_KIND_NAVIGATION",
    "NETWORK_KIND_PROXY_BYPASS",
    "NETWORK_KIND_REDIRECT",
    "NETWORK_KIND_WEBSOCKET",
    "RECONCILE_APPLIED",
    "RECONCILE_NOT_APPLIED",
    "RECONCILE_PARTIAL",
    "RECONCILE_STATES",
    "RECONCILE_UNKNOWN",
    "RETRYABLE_STATES",
    "SYSTEMS",
    "SYSTEM_ACCOUNTING",
    "SYSTEM_CRM",
    "SYSTEM_EMAIL",
    "SYSTEM_STORAGE",
    "SYSTEM_WEBHOOK",
    "BrowserSession",
    "EffectIntent",
    "EffectRecord",
    "NetworkDecision",
    "NetworkRequest",
    "SyntheticBrowserSessionOracle",
    "SyntheticExternalOracle",
    "SyntheticNetworkPolicy",
    "WebhookEvent",
]
