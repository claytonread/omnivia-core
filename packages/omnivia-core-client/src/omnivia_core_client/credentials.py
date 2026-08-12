"""Turning a name for a credential into a credential, bound to one endpoint origin.

**This module holds no credential source and adds none.** It defines no token
format, no issuer, no store, no broker, no keychain, no account database, and it
reads no environment variable, no argument vector, no configuration file and no
file beside anything. Every one of those is a separate, separately approved
decision, and inventing one here would be inventing the security design it
belongs to. What is here is the seam: the host injects a
:data:`CredentialResolver`, and this module decides what a *reference* may look
like, what an answer may look like, what happens when the answer does not come,
and -- the point of the whole module -- that an answer given for one endpoint is
never presented to another.

Three types, and the distinction between the first two is the load-bearing one.

:class:`CredentialReference` is a **name**, not a secret. It is what a
descriptor, a configuration file, a command line or a log may legitimately
carry, and its whole grammar exists to keep it that way: the accepted character
set admits letters, digits, ``.``, ``_`` and ``-`` and nothing else, so a
reference cannot spell a control character, a space, an ``@`` or ``:`` in the
userinfo position, a ``?``, ``&``, ``=`` or ``#`` from a query, a ``%`` escape,
or a ``/`` or ``\\`` from a path. Two further rules close what the character set
alone leaves open: a reference may not contain ``..`` (parent-directory
traversal is path semantics whatever the separator would have been), and it may
not be a compact JWS -- ``ey...`` followed by two dot-separated segments -- which
is a token pasted where the *name* of a token belongs. That last one is the only
rule here that looks at content rather than shape, and it is worth its narrowness:
the failure it catches is somebody putting the secret in the field that gets
logged.

:class:`Credential` is the **secret**, and it is opaque by construction.
``repr`` and ``str`` are a fixed redaction, so an f-string, a ``%r``, a
``pprint``, a traceback frame's local rendering and a structured log all see the
same nothing. :meth:`Credential.reveal` is the only way to the material, and it
is named to make reading it a deliberate act that shows up in review. The
accepted secret is visible ASCII with no space -- which is what an
``Authorization`` header field value may carry -- so a secret with a ``\\r`` or a
``\\n`` in it is refused here rather than becoming header injection at a socket.

:class:`CredentialCache` is where the two meet, and it is the only thing that
calls the resolver. The cache key is ``(origin, reference)``, and the origin is
required to be a *normalized* origin -- lowercase scheme and host, an explicit
port, an IPv6 literal in brackets -- so a caller cannot make two spellings of one
endpoint into two cache entries, and, far more importantly, cannot make a
credential resolved for one endpoint answer for another. **A credential resolved
for one origin is never presented to a second one.** That is a property of the
key rather than a rule a caller has to remember.

**Nothing a resolver does escapes this module.** The resolver is handed the
reference and the origin, which are the two things a host needs to decide, and
it is called behind :func:`_resolve`. Whatever it raises is dropped rather than
chained: a store quoting the key it looked up, a decoder quoting the bytes it
could not parse, an HTTP client quoting the URL it was given -- any of those
would put a credential, a store location or an endpoint into an exception this
package then hands to application code, and ``__cause__`` and ``__context__``
keep it reachable even when nothing renders it. The four outcomes in
:mod:`~omnivia_core_client.errors` are raised *after* the handler ends, from
prepared sentences, so there is no exception being handled and nothing to chain
to. ``BaseException`` is deliberately not caught: ``KeyboardInterrupt`` and
``SystemExit`` are this process stopping, not a credential failing to resolve.

Standard library only, and no import of any sibling distribution.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable
from typing import Final, NoReturn, TypeAlias

from omnivia_core_client.errors import (
    CredentialDeniedError,
    CredentialInvalidError,
    CredentialMissingError,
    CredentialUnavailableError,
)

__all__ = [
    "DEFAULT_CREDENTIAL_TTL_SECONDS",
    "MAXIMUM_CREDENTIAL_CHARACTERS",
    "MAXIMUM_REFERENCE_CHARACTERS",
    "Credential",
    "CredentialCache",
    "CredentialReference",
    "CredentialResolver",
]

MAXIMUM_REFERENCE_CHARACTERS: Final = 256
"""Longest accepted credential reference, **inclusive**.

A bound rather than no bound, for the same reason every other bound in this
package exists: a reference arrives from configuration this process did not
write, and a name is a name at 256 characters."""

MAXIMUM_CREDENTIAL_CHARACTERS: Final = 4096
"""Longest accepted secret, **inclusive**.

Not a statement about any token format -- this module defines none. It is the
bound on what will be built into one ``Authorization`` header, so a resolver
that answers with a megabyte fails here rather than at a peer that would refuse
the request anyway."""

DEFAULT_CREDENTIAL_TTL_SECONDS: Final = 60.0
"""How long a resolved credential may be reused before the resolver is asked again.

Short on purpose. The cache exists so a burst of calls to one endpoint does not
become a burst of calls to a credential store, not so a process can hold a
credential for its lifetime: a host that revokes or rotates one needs the change
to take effect without a restart, and an entry that outlives the decision behind
it is exactly what stops that."""

_REFERENCE_PATTERN: Final = re.compile(
    rf"\A[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAXIMUM_REFERENCE_CHARACTERS - 1}}}\Z"
)
"""The accepted reference grammar, as an allowlist.

An allowlist rather than a list of banned characters, because a denylist only
refuses the syntax somebody thought of. The leading character is restricted to a
letter or digit so a reference cannot open with ``.``, ``-`` or any other
punctuation that reads as a relative path or an option."""

_COMPACT_TOKEN_PATTERN: Final = re.compile(r"\Aey[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.")
"""What a compact JWS looks like from its first bytes.

``ey`` is the base64url rendering of ``{"``, so every JSON-header token begins
with it, followed by dot-separated base64url segments. Matched on the prefix
rather than the whole value because the point is to recognise the *shape* early,
not to validate a token this module has no business validating."""

_SECRET_PATTERN: Final = re.compile(
    rf"\A[\x21-\x7e]{{1,{MAXIMUM_CREDENTIAL_CHARACTERS}}}\Z"
)
"""What may be carried in an ``Authorization`` header field value.

Visible ASCII with no space and no control character. That is narrower than
RFC 9110 allows for a field value in general and exactly right for a bearer
credential, and it is what makes ``\\r\\n`` in a secret a refusal here instead of
a request-splitting defect at a socket."""

_ORIGIN_PATTERN: Final = re.compile(
    r"\Ahttps?://(?:\[[0-9a-f:.]+\]|[a-z0-9.-]+):(?P<port>[1-9][0-9]{0,4})\Z"
)
"""A *normalized* endpoint origin: lowercase, explicit port, IPv6 in brackets.

Not a URL parser and not a second endpoint grammar -- the endpoint parser lives
in :mod:`~omnivia_core_client.http_transport` and this admits only what that
parser produces. It is here so the cache key cannot be an arbitrary string: two
spellings of one endpoint would be two entries, and, the failure that matters,
an unnormalized origin would let a credential bound to one endpoint be looked up
under another."""

_REDACTED: Final = "<credential redacted>"
"""What a credential renders as, everywhere. There is no verbose form, no
partial reveal and no length hint: a prefix or a length is still information
about a secret, and the number of characters in one is not nothing."""

_NOT_AN_ADMISSIBLE_REFERENCE: Final = (
    "credential reference is outside the accepted grammar, which is 1 to "
    f"{MAXIMUM_REFERENCE_CHARACTERS} characters, opening with a letter or digit "
    "and continuing with letters, digits, '.', '_' or '-' only, carrying no '..' "
    "and not spelling a compact token"
)
"""Why a reference was refused. Names the *rule set*, never the reference and
never which rule it broke: a reference is not a secret, but it is caller-supplied
text and this package quotes none of that in a diagnostic."""

_NOT_AN_ADMISSIBLE_SECRET: Final = (
    "credential secret is outside the accepted form, which is 1 to "
    f"{MAXIMUM_CREDENTIAL_CHARACTERS} visible ASCII characters with no space and "
    "no control character"
)

_NOT_A_NORMALIZED_ORIGIN: Final = (
    "credential origin is not a normalized endpoint origin: 'http' or 'https', a "
    "lowercase host or a bracketed IPv6 literal, and an explicit port"
)


#: What a credential reference resolves to, injected by the host.
#:
#: Takes the reference and the **normalized endpoint origin** the credential
#: would be presented to, and returns the :class:`Credential` the host is willing
#: to release for that pair, or ``None`` when it holds none. The origin is an
#: argument rather than context the resolver has to remember, because "may this
#: reference be used against *this* endpoint" is the question only the host can
#: answer and it cannot answer it without being asked.
#:
#: Raise :class:`~omnivia_core_client.errors.CredentialDeniedError` to refuse
#: releasing a credential that does exist. Anything else raised is reported as
#: :class:`~omnivia_core_client.errors.CredentialUnavailableError` and is dropped
#: without being chained -- see this module's docstring.
CredentialResolver: TypeAlias = Callable[
    ["CredentialReference", str], "Credential | None"
]


def _raise_invalid_reference() -> NoReturn:
    raise CredentialInvalidError(_NOT_AN_ADMISSIBLE_REFERENCE)


def _raise_invalid_secret() -> NoReturn:
    raise CredentialInvalidError(_NOT_AN_ADMISSIBLE_SECRET)


def _raise_unnormalized_origin() -> NoReturn:
    raise CredentialInvalidError(_NOT_A_NORMALIZED_ORIGIN)


#: Raised from outside the ``except`` blocks that decide on them, never inside.
#:
#: ``raise X from None`` inside a handler clears ``__cause__`` and leaves
#: ``__context__`` pointing at the exception being handled -- which here is
#: whatever an injected resolver raised, one attribute access from anything that
#: logs the failure. Raising after the handler ends means there is no exception
#: being handled at all. Same shape as ``local_ipc.py`` and ``discovery.py``, and
#: ``scripts/check-raise-discipline.py`` is what keeps it that way.


def _raise_missing() -> NoReturn:
    raise CredentialMissingError(
        "no credential is held for this reference and endpoint"
    )


def _raise_denied() -> NoReturn:
    raise CredentialDeniedError("the credential was not released for this endpoint")


def _raise_unavailable() -> NoReturn:
    raise CredentialUnavailableError("the credential could not be resolved")


def _raise_invalid_answer() -> NoReturn:
    raise CredentialInvalidError("the resolver answered with no usable credential")


class CredentialReference:
    """An opaque name for a credential. Never the credential itself.

    Immutable, hashable and comparable by value, so it can be a dictionary key
    and can be compared without anyone reaching for the string inside it.
    Written as a plain slots class rather than a frozen dataclass because the
    validated form is the only form: there is no constructor path that produces
    an instance the grammar did not admit, and no ``dataclasses.replace`` that
    would bypass one.

    ``repr`` shows the reference, deliberately. It is a name -- that is the whole
    distinction this module is built on -- and hiding it would make a
    configuration mistake unreadable while protecting nothing.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            _raise_invalid_reference()
        if _REFERENCE_PATTERN.match(value) is None:
            _raise_invalid_reference()
        if ".." in value:
            _raise_invalid_reference()
        if _COMPACT_TOKEN_PATTERN.match(value) is not None:
            _raise_invalid_reference()
        self._value = value

    @property
    def value(self) -> str:
        """The accepted reference text."""
        return self._value

    def __repr__(self) -> str:
        return f"CredentialReference({self._value!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CredentialReference):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash((CredentialReference, self._value))


class Credential:
    """Secret material, in the only carrier that may hold it in this package.

    Opaque by construction rather than by convention. ``repr`` and ``str`` are
    the same fixed redaction, which is what makes an f-string, a ``%r``, a
    ``pprint`` and a structured log all safe by default;
    :meth:`reveal` is the one way to the material and is named so that reading
    it is visible in a diff.

    Equality and hashing are by *identity*, inherited rather than defined. Two
    credentials holding the same secret comparing unequal is the right answer
    here: a value comparison over secret material invites using ``==`` on
    credentials, and this package has nothing that needs to.
    """

    __slots__ = ("_secret",)

    def __init__(self, secret: str) -> None:
        if not isinstance(secret, str):
            _raise_invalid_secret()
        if _SECRET_PATTERN.match(secret) is None:
            _raise_invalid_secret()
        self._secret = secret

    def reveal(self) -> str:
        """The secret material, for the one caller that must put it on a wire."""
        return self._secret

    def __repr__(self) -> str:
        return _REDACTED

    def __str__(self) -> str:
        return _REDACTED


def _resolve(
    resolver: CredentialResolver, reference: CredentialReference, origin: str
) -> Credential:
    """Call the resolver so nothing it raises or returns can carry secrets onward.

    The four outcomes are decided here and raised below, outside every handler.
    ``CredentialDeniedError`` is the one a host raises on purpose, so it is
    recognised by *type* -- but the instance is dropped like every other, because
    a host's own denial message is written by the host and this package cannot
    promise it is free of the reference, the origin or the store behind it.
    """
    outcome = ""
    answer: object = None
    try:
        answer = resolver(reference, origin)
    except CredentialDeniedError:
        outcome = "denied"
    except CredentialInvalidError:
        outcome = "invalid"
    except Exception:  # noqa: BLE001 -- deliberate; see the module docstring
        outcome = "unavailable"
    if outcome == "denied":
        _raise_denied()
    if outcome == "invalid":
        _raise_invalid_answer()
    if outcome == "unavailable":
        _raise_unavailable()
    if answer is None:
        _raise_missing()
    if not isinstance(answer, Credential):
        # Checked rather than trusted: an injected resolver can return anything,
        # and a truthy non-credential would otherwise reach the header builder.
        _raise_invalid_answer()
    return answer


class CredentialCache:
    """The one caller of the resolver, keyed by ``(origin, reference)``.

    **Cross-origin reuse is not prevented here, it is unrepresentable here.** The
    origin is half the key, so a lookup for a second endpoint cannot find a first
    endpoint's entry; there is no code path that returns a credential resolved
    for one origin to a caller asking about another, and no flag that would turn
    one on.

    Bounded in time and clearable on demand, which is what a credential cache
    owes a process that can be shut down: :meth:`clear` drops every entry, and an
    entry that is older than ``ttl_seconds`` is discarded and re-resolved rather
    than served. ``ttl_seconds=0`` caches nothing at all, which is the honest way
    to ask for no caching rather than a second class that does not cache.

    The resolver is called with the lock released. Holding a lock across an
    injected callable would let a resolver that blocks -- a store that hangs, a
    broker that never answers -- hold every other call to every other endpoint
    behind it, and the cost of releasing it is that two threads racing on one
    cold key may both resolve. That is one extra resolver call, and resolution
    is idempotent by contract.
    """

    __slots__ = ("_clock", "_entries", "_lock", "_resolver", "_ttl_seconds")

    def __init__(
        self,
        resolver: CredentialResolver,
        *,
        ttl_seconds: float = DEFAULT_CREDENTIAL_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        normalized_ttl: float | None = None
        try:
            normalized_ttl = float(ttl_seconds)
        except (TypeError, ValueError, OverflowError):
            pass
        if (
            normalized_ttl is None
            or not math.isfinite(normalized_ttl)
            or normalized_ttl < 0
        ):
            raise ValueError("ttl_seconds must be a finite non-negative number")
        self._resolver = resolver
        self._ttl_seconds = normalized_ttl
        self._clock = clock
        self._entries: dict[tuple[str, str], tuple[float, Credential]] = {}
        self._lock = threading.Lock()

    @property
    def ttl_seconds(self) -> float:
        """How long an entry may be served before the resolver is asked again."""
        return self._ttl_seconds

    def credential_for(self, reference: CredentialReference, origin: str) -> Credential:
        """The credential for this reference at this origin, cached or freshly resolved."""
        if not isinstance(reference, CredentialReference):
            _raise_invalid_reference()
        origin_match = (
            _ORIGIN_PATTERN.match(origin) if isinstance(origin, str) else None
        )
        if origin_match is None or int(origin_match.group("port")) > 65535:
            _raise_unnormalized_origin()
        key = (origin, reference.value)
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if entry[0] > now:
                    return entry[1]
                del self._entries[key]
        credential = _resolve(self._resolver, reference, origin)
        if self._ttl_seconds > 0:
            with self._lock:
                self._entries[key] = (now + self._ttl_seconds, credential)
        return credential

    def clear(self) -> None:
        """Drop every cached credential. Call this at shutdown."""
        with self._lock:
            self._entries.clear()

    def __repr__(self) -> str:
        """Says how many entries there are and nothing about any of them.

        Not the default ``object`` repr, because a count is genuinely useful when
        reading a shutdown path -- and not a dataclass-style one either, which
        would render the entries and with them every origin this process holds a
        credential for.
        """
        with self._lock:
            count = len(self._entries)
        return f"CredentialCache(entries={count}, ttl_seconds={self._ttl_seconds})"
