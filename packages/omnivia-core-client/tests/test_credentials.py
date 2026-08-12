"""The credential seam: what a reference may be, and what a resolver cannot leak.

Three properties carry this suite, and each is asserted structurally rather than
by inspecting a rendered message.

**A reference is a name and a credential is a secret.** The grammar matrix below
is the whole of what a reference may be; everything a secret must never do is
asserted on ``repr``, ``str`` and the exception surface.

**A credential is bound to one origin.** Not "should not be reused" -- the cache
is asked for the same reference at a second origin and the resolver is observed
being called again, so reuse is shown to be unrepresentable rather than merely
undone.

**Nothing a resolver does escapes.** The hostile resolvers below put the secret,
the reference, the origin and a store path in every place an exception can carry
one, and the assertions walk ``args``, ``__cause__`` and ``__context__`` rather
than reading ``str(error)``: a chained exception one attribute access away is the
defect, and a traceback that happens not to render it is not the fix.
"""

from __future__ import annotations

import ast
import inspect
import math

import pytest
from omnivia_core_client import (
    DEFAULT_CREDENTIAL_TTL_SECONDS,
    MAXIMUM_CREDENTIAL_CHARACTERS,
    MAXIMUM_REFERENCE_CHARACTERS,
    ClientError,
    Credential,
    CredentialCache,
    CredentialDeniedError,
    CredentialError,
    CredentialInvalidError,
    CredentialMissingError,
    CredentialReference,
    CredentialUnavailableError,
)

ORIGIN = "https://core.example:443"
OTHER_ORIGIN = "https://other.example:443"
SECRET = "s3cret-material-nobody-should-see"
STORE = "/var/lib/omnivia/credentials.db"


def _cache(resolver: object, **kwargs: object) -> CredentialCache:
    return CredentialCache(resolver, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The reference grammar
# --------------------------------------------------------------------------


ACCEPTED_REFERENCES = [
    ("one_character", "a"),
    ("digit", "0"),
    ("kebab", "core-service-credential"),
    ("snake", "core_service_credential"),
    ("dotted", "omnivia.core.default"),
    ("mixed", "Core-Service_v2.default"),
    ("at_the_maximum", "a" * MAXIMUM_REFERENCE_CHARACTERS),
    ("starts_like_a_token_but_is_not_one", "eyebrow-credential"),
]

REJECTED_REFERENCES = [
    ("empty", ""),
    ("over_the_maximum", "a" * (MAXIMUM_REFERENCE_CHARACTERS + 1)),
    # control characters
    ("nul", "cred\x00ential"),
    ("newline", "cred\nential"),
    ("carriage_return", "cred\rential"),
    ("tab", "cred\tential"),
    ("delete", "cred\x7fential"),
    ("c1_control", "cred\x85ential"),
    # whitespace
    ("space", "core credential"),
    ("leading_space", " credential"),
    ("trailing_space", "credential "),
    ("non_breaking_space", "core\xa0credential"),
    # userinfo and query syntax
    ("userinfo_at", "user@host"),
    ("userinfo_colon", "user:password"),
    ("query", "cred?token=abc"),
    ("query_join", "cred&token=abc"),
    ("assignment", "token=abc"),
    ("fragment", "cred#abc"),
    ("percent_escape", "cred%2Fential"),
    ("scheme", "https://core.example/cred"),
    # inline token semantics
    ("compact_jws", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"),
    ("compact_jws_prefix_only", "eyXX.YY.ZZ"),
    ("bearer_header", "Bearer abc"),
    # filesystem path semantics
    ("absolute_posix_path", "/etc/omnivia/credential"),
    ("relative_path", "creds/default"),
    ("windows_path", "C:\\creds\\default"),
    ("backslash", "creds\\default"),
    ("parent_traversal", "creds..default"),
    ("dot_dot", ".."),
    ("home", "~/creds"),
    ("leading_dot", ".hidden-credential"),
    ("leading_dash", "-credential"),
]


def _refusal(value: str) -> str:
    """The message one rejected reference produces."""
    with pytest.raises(CredentialInvalidError) as caught:
        CredentialReference(value)
    return str(caught.value)


#: Taken from a rejection whose value shares nothing with the message, so the
#: fixed sentence every other rejection is held to is not itself derived from a
#: value that could have been quoted into it.
REFERENCE_REFUSAL = _refusal("\x00")


@pytest.mark.parametrize(
    "value",
    [value for _, value in ACCEPTED_REFERENCES],
    ids=[n for n, _ in ACCEPTED_REFERENCES],
)
def test_an_accepted_reference_keeps_exactly_what_it_was_given(value: str) -> None:
    assert CredentialReference(value).value == value


@pytest.mark.parametrize(
    "value",
    [value for _, value in REJECTED_REFERENCES],
    ids=[n for n, _ in REJECTED_REFERENCES],
)
def test_a_rejected_reference_is_refused_without_being_quoted(value: str) -> None:
    """Refused, and the refusal does not repeat what it refused.

    Asserted as *equality with a fixed sentence* rather than as "the value is not
    a substring of the message", which is the check that looks right and is not:
    the rule text legitimately contains ``..`` and the word ``credential``, so a
    substring test both false-alarms on those and would pass a message that
    quoted some other part of the value. A message that is byte-identical for
    every rejection cannot have quoted any of them.

    A reference is not a secret, but it is caller-supplied text, and this package
    quotes none of that in a diagnostic -- among other reasons because the one
    reference most likely to be rejected is a secret somebody pasted into the
    name field.
    """
    with pytest.raises(CredentialInvalidError) as caught:
        CredentialReference(value)

    error = caught.value
    assert isinstance(error, ClientError)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert str(error) == REFERENCE_REFUSAL
    assert error.args == (REFERENCE_REFUSAL,)


@pytest.mark.parametrize("value", [None, 7, b"credential", ["credential"]])
def test_a_reference_that_is_not_a_string_is_refused_as_one(value: object) -> None:
    """A ``TypeError`` here would escape a caller who wrote ``except ClientError``."""
    with pytest.raises(CredentialInvalidError):
        CredentialReference(value)  # type: ignore[arg-type]


def test_the_refusal_names_the_rule_set_rather_than_the_rule() -> None:
    """One sentence for every rejection, so the message cannot become an oracle.

    Naming *which* rule a reference broke tells the writer of a hostile
    reference exactly how to get past the next one, and naming it for a value
    that turned out to be a pasted secret would be the disclosure this whole
    module is about.
    """
    messages = {_refusal(value) for _, value in REJECTED_REFERENCES}
    assert messages == {REFERENCE_REFUSAL}


def test_references_compare_and_hash_by_value() -> None:
    """It is half a cache key, so value semantics are load-bearing rather than tidy."""
    one = CredentialReference("core.default")
    same = CredentialReference("core.default")
    other = CredentialReference("core.other")

    assert one == same
    assert hash(one) == hash(same)
    assert one != other
    assert one != "core.default"
    assert len({one, same, other}) == 2


def test_a_reference_renders_as_itself() -> None:
    """Deliberately *not* redacted: it is a name, and hiding it would make a
    configuration mistake unreadable while protecting nothing."""
    assert (
        repr(CredentialReference("core.default"))
        == "CredentialReference('core.default')"
    )


# --------------------------------------------------------------------------
# The opaque carrier
# --------------------------------------------------------------------------


def test_a_credential_reveals_nothing_through_str_or_repr() -> None:
    credential = Credential(SECRET)

    assert repr(credential) == "<credential redacted>"
    assert str(credential) == "<credential redacted>"
    assert f"{credential}" == "<credential redacted>"
    assert f"{credential!r}" == "<credential redacted>"
    assert SECRET not in repr(credential)
    assert SECRET not in str([credential])
    assert SECRET not in str({"credential": credential})


def test_a_credential_reveals_nothing_through_its_length_either() -> None:
    """No prefix, no suffix, no character count. A length is information about a
    secret, and the number of characters in one is not nothing."""
    short = Credential("a")
    long = Credential("a" * 512)

    assert repr(short) == repr(long)


def test_reveal_is_the_one_way_to_the_material() -> None:
    assert Credential(SECRET).reveal() == SECRET


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("empty", ""),
        ("space", "cred ential"),
        ("carriage_return_newline", "credential\r\nX-Injected: yes"),
        ("newline", "credential\n"),
        ("tab", "creden\ttial"),
        ("nul", "creden\x00tial"),
        ("non_ascii", "crédential"),
        ("over_the_maximum", "a" * (MAXIMUM_CREDENTIAL_CHARACTERS + 1)),
    ],
)
def test_a_secret_that_could_not_travel_in_a_header_is_refused(
    name: str, secret: str
) -> None:
    """Header injection is refused here, one layer before a socket could see it."""
    with pytest.raises(CredentialInvalidError) as caught:
        Credential(secret)

    assert secret == "" or secret not in str(caught.value)
    assert caught.value.__context__ is None


def test_a_secret_at_the_maximum_is_accepted() -> None:
    secret = "a" * MAXIMUM_CREDENTIAL_CHARACTERS
    assert Credential(secret).reveal() == secret


@pytest.mark.parametrize("secret", [None, 7, b"credential"])
def test_a_secret_that_is_not_a_string_is_refused_as_one(secret: object) -> None:
    with pytest.raises(CredentialInvalidError):
        Credential(secret)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The four outcomes
# --------------------------------------------------------------------------


def test_a_resolved_credential_comes_back() -> None:
    seen: list[tuple[CredentialReference, str]] = []

    def resolver(reference: CredentialReference, origin: str) -> Credential:
        seen.append((reference, origin))
        return Credential(SECRET)

    reference = CredentialReference("core.default")
    credential = _cache(resolver, ttl_seconds=0).credential_for(reference, ORIGIN)

    assert credential.reveal() == SECRET
    assert seen == [(reference, ORIGIN)]


def test_the_resolver_is_asked_about_the_origin_it_would_be_presented_to() -> None:
    """The origin is an argument rather than context the resolver must remember.

    "May this reference be used against *this* endpoint" is the question only the
    host can answer, and it cannot answer it without being asked.
    """
    asked: list[str] = []

    def resolver(reference: CredentialReference, origin: str) -> Credential | None:
        asked.append(origin)
        return Credential(SECRET) if origin == ORIGIN else None

    cache = _cache(resolver, ttl_seconds=0)
    reference = CredentialReference("core.default")

    assert cache.credential_for(reference, ORIGIN).reveal() == SECRET
    with pytest.raises(CredentialMissingError):
        cache.credential_for(reference, OTHER_ORIGIN)
    assert asked == [ORIGIN, OTHER_ORIGIN]


def test_nothing_resolved_is_missing() -> None:
    with pytest.raises(CredentialMissingError):
        _cache(lambda reference, origin: None).credential_for(
            CredentialReference("core.default"), ORIGIN
        )


def test_a_host_that_refuses_is_denied() -> None:
    def resolver(reference: CredentialReference, origin: str) -> Credential:
        raise CredentialDeniedError(f"{reference.value} is not released to {origin}")

    with pytest.raises(CredentialDeniedError) as caught:
        _cache(resolver).credential_for(CredentialReference("core.default"), ORIGIN)

    assert "core.default" not in str(caught.value)
    assert ORIGIN not in str(caught.value)


def test_a_resolver_that_fails_is_unavailable() -> None:
    def resolver(reference: CredentialReference, origin: str) -> Credential:
        raise OSError(f"could not open {STORE}")

    with pytest.raises(CredentialUnavailableError) as caught:
        _cache(resolver).credential_for(CredentialReference("core.default"), ORIGIN)

    assert STORE not in str(caught.value)


@pytest.mark.parametrize(
    ("name", "answer"),
    [
        ("a_bare_string", SECRET),
        ("a_truthy_object", object()),
        ("a_number", 1),
        ("a_mapping", {"secret": SECRET}),
    ],
)
def test_an_answer_that_is_not_a_credential_is_invalid(
    name: str, answer: object
) -> None:
    """Checked rather than trusted. A truthy non-credential would otherwise reach
    the header builder and fail there, with the wrong error and a secret in it."""
    with pytest.raises(CredentialInvalidError):
        _cache(lambda reference, origin: answer).credential_for(
            CredentialReference("core.default"), ORIGIN
        )


def test_all_four_outcomes_are_one_family() -> None:
    for outcome in (
        CredentialMissingError,
        CredentialDeniedError,
        CredentialUnavailableError,
        CredentialInvalidError,
    ):
        assert issubclass(outcome, CredentialError)
        assert issubclass(outcome, ClientError)


# --------------------------------------------------------------------------
# Hostile resolvers
# --------------------------------------------------------------------------


LEAKS = (SECRET, "core.default", ORIGIN, STORE, "core.example")


def _assert_nothing_leaked(error: BaseException) -> None:
    """Walk the whole exception surface, not the rendered message.

    ``args``, ``__cause__`` and ``__context__`` are each one attribute access
    from anything that logs, serializes or diffs the error a caller caught, and
    a chained exception whose message quotes the store is the defect whether or
    not the default traceback rendering shows it.
    """
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "\n".join([str(error), repr(error), *(repr(arg) for arg in error.args)])
    for leak in LEAKS:
        assert leak not in rendered, leak


HOSTILE_RESOLVERS = {
    "raises_with_the_secret": lambda reference, origin: (_ for _ in ()).throw(
        RuntimeError(f"token {SECRET} rejected by {STORE}")
    ),
    "raises_a_denial_with_the_secret": lambda reference, origin: (_ for _ in ()).throw(
        CredentialDeniedError(f"{SECRET} is not released for {origin}")
    ),
    "raises_an_invalid_with_the_secret": lambda reference, origin: (
        _ for _ in ()
    ).throw(CredentialInvalidError(f"{SECRET} at {STORE}")),
    "returns_the_bare_secret": lambda reference, origin: SECRET,
    "returns_a_dict_of_everything": lambda reference, origin: {
        "secret": SECRET,
        "origin": origin,
        "store": STORE,
    },
}


@pytest.mark.parametrize("name", sorted(HOSTILE_RESOLVERS))
def test_a_hostile_resolver_leaks_nothing_into_the_failure(name: str) -> None:
    with pytest.raises(CredentialError) as caught:
        _cache(HOSTILE_RESOLVERS[name]).credential_for(
            CredentialReference("core.default"), ORIGIN
        )

    _assert_nothing_leaked(caught.value)


def test_a_resolver_that_chains_its_own_failure_still_leaks_nothing() -> None:
    """The chained case specifically: ``raise X from Y`` inside the resolver."""

    def resolver(reference: CredentialReference, origin: str) -> Credential:
        try:
            raise FileNotFoundError(STORE)
        except FileNotFoundError as error:
            raise RuntimeError(f"{reference.value} at {origin}") from error

    with pytest.raises(CredentialUnavailableError) as caught:
        _cache(resolver).credential_for(CredentialReference("core.default"), ORIGIN)

    _assert_nothing_leaked(caught.value)


@pytest.mark.parametrize("stopping", [KeyboardInterrupt, SystemExit])
def test_the_process_stopping_is_not_a_credential_failure(
    stopping: type[BaseException],
) -> None:
    """``BaseException`` is deliberately not caught: those two are this process
    stopping, not a credential failing to resolve."""

    def resolver(reference: CredentialReference, origin: str) -> Credential:
        raise stopping()

    with pytest.raises(stopping):
        _cache(resolver).credential_for(CredentialReference("core.default"), ORIGIN)


# --------------------------------------------------------------------------
# Origin binding and the cache
# --------------------------------------------------------------------------


class _CountingResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, reference: CredentialReference, origin: str) -> Credential:
        self.calls.append((reference.value, origin))
        return Credential(f"{SECRET}-{origin}")


def test_a_credential_resolved_for_one_origin_is_never_served_to_another() -> None:
    """The property this module exists for, asserted on both halves.

    The second origin gets its *own* resolver call and its own secret, and the
    first origin's cached entry is untouched by it.
    """
    resolver = _CountingResolver()
    cache = _cache(resolver, ttl_seconds=1000.0, clock=lambda: 0.0)
    reference = CredentialReference("core.default")

    first = cache.credential_for(reference, ORIGIN)
    second = cache.credential_for(reference, OTHER_ORIGIN)

    assert first.reveal() != second.reveal()
    assert first.reveal().endswith(ORIGIN)
    assert second.reveal().endswith(OTHER_ORIGIN)
    assert resolver.calls == [
        ("core.default", ORIGIN),
        ("core.default", OTHER_ORIGIN),
    ]
    # And the first is still the first, after the second existed.
    assert cache.credential_for(reference, ORIGIN).reveal() == first.reveal()
    assert len(resolver.calls) == 2


def test_two_references_at_one_origin_are_two_entries() -> None:
    resolver = _CountingResolver()
    cache = _cache(resolver, ttl_seconds=1000.0, clock=lambda: 0.0)

    cache.credential_for(CredentialReference("core.default"), ORIGIN)
    cache.credential_for(CredentialReference("core.other"), ORIGIN)

    assert [call[0] for call in resolver.calls] == ["core.default", "core.other"]


def test_a_cached_credential_is_served_without_asking_again() -> None:
    resolver = _CountingResolver()
    cache = _cache(resolver, ttl_seconds=60.0, clock=lambda: 0.0)
    reference = CredentialReference("core.default")

    for _ in range(5):
        cache.credential_for(reference, ORIGIN)

    assert len(resolver.calls) == 1


def test_an_entry_older_than_the_lifetime_is_resolved_again() -> None:
    """Lifetime-bounded rather than held for the life of the process: a host that
    revokes or rotates a credential needs that to take effect without a restart."""
    now = 0.0
    resolver = _CountingResolver()
    cache = _cache(resolver, ttl_seconds=60.0, clock=lambda: now)
    reference = CredentialReference("core.default")

    cache.credential_for(reference, ORIGIN)
    now = 59.0
    cache.credential_for(reference, ORIGIN)
    assert len(resolver.calls) == 1

    now = 60.0
    cache.credential_for(reference, ORIGIN)
    assert len(resolver.calls) == 2


def test_a_zero_lifetime_caches_nothing_at_all() -> None:
    resolver = _CountingResolver()
    cache = _cache(resolver, ttl_seconds=0)
    reference = CredentialReference("core.default")

    cache.credential_for(reference, ORIGIN)
    cache.credential_for(reference, ORIGIN)

    assert len(resolver.calls) == 2


def test_clear_drops_every_entry() -> None:
    resolver = _CountingResolver()
    cache = _cache(resolver, ttl_seconds=1000.0, clock=lambda: 0.0)
    reference = CredentialReference("core.default")

    cache.credential_for(reference, ORIGIN)
    cache.credential_for(reference, OTHER_ORIGIN)
    assert len(resolver.calls) == 2

    cache.clear()

    cache.credential_for(reference, ORIGIN)
    cache.credential_for(reference, OTHER_ORIGIN)
    assert len(resolver.calls) == 4


def test_a_failed_resolution_is_not_cached() -> None:
    """A denial is a decision, not an answer to keep: a host that changes its mind
    would otherwise be ignored until the entry expired."""
    calls = 0

    def resolver(reference: CredentialReference, origin: str) -> Credential | None:
        nonlocal calls
        calls += 1
        return None

    cache = _cache(resolver, ttl_seconds=1000.0, clock=lambda: 0.0)
    reference = CredentialReference("core.default")

    for _ in range(3):
        with pytest.raises(CredentialMissingError):
            cache.credential_for(reference, ORIGIN)

    assert calls == 3


def test_the_cache_renders_a_count_and_nothing_else() -> None:
    cache = _cache(_CountingResolver(), ttl_seconds=1000.0, clock=lambda: 0.0)
    cache.credential_for(CredentialReference("core.default"), ORIGIN)

    rendered = repr(cache)
    assert "entries=1" in rendered
    assert ORIGIN not in rendered
    assert "core.default" not in rendered
    assert SECRET not in rendered


def test_the_default_lifetime_is_short_and_stated() -> None:
    assert DEFAULT_CREDENTIAL_TTL_SECONDS == 60.0
    assert _cache(_CountingResolver()).ttl_seconds == DEFAULT_CREDENTIAL_TTL_SECONDS


def test_a_negative_lifetime_is_refused() -> None:
    with pytest.raises(ValueError):
        _cache(_CountingResolver(), ttl_seconds=-1.0)


@pytest.mark.parametrize("ttl_seconds", [math.inf, -math.inf, math.nan])
def test_a_non_finite_lifetime_is_refused(ttl_seconds: float) -> None:
    """No spelling of infinity may turn a bounded cache into a process cache."""
    with pytest.raises(ValueError):
        _cache(_CountingResolver(), ttl_seconds=ttl_seconds)


# --------------------------------------------------------------------------
# The origin must be a normalized one
# --------------------------------------------------------------------------


ACCEPTED_ORIGINS = [
    "http://127.0.0.1:8080",
    "https://core.example:443",
    "https://core.example:8443",
    "https://[::1]:443",
    "http://[::1]:8080",
    "https://sub.core.example:443",
]

REJECTED_ORIGINS = [
    ("no_port", "https://core.example"),
    ("uppercase_host", "https://Core.Example:443"),
    ("uppercase_scheme", "HTTPS://core.example:443"),
    ("unbracketed_ipv6", "https://::1:443"),
    ("trailing_path", "https://core.example:443/"),
    ("query", "https://core.example:443?a=b"),
    ("userinfo", "https://user:pass@core.example:443"),
    ("other_scheme", "unix:///run/core.sock"),
    ("bare_host", "core.example:443"),
    ("empty", ""),
    ("newline", "https://core.example:443\n"),
    ("zero_port", "https://core.example:0"),
    ("port_out_of_range", "https://core.example:65536"),
]


@pytest.mark.parametrize("origin", ACCEPTED_ORIGINS)
def test_a_normalized_origin_is_accepted(origin: str) -> None:
    resolver = _CountingResolver()
    _cache(resolver, ttl_seconds=0).credential_for(
        CredentialReference("core.default"), origin
    )
    assert resolver.calls == [("core.default", origin)]


@pytest.mark.parametrize(
    "origin",
    [origin for _, origin in REJECTED_ORIGINS],
    ids=[n for n, _ in REJECTED_ORIGINS],
)
def test_an_unnormalized_origin_never_reaches_the_resolver(origin: str) -> None:
    """The key has to be canonical or the binding is not one.

    Two spellings of one endpoint would be two entries -- wasteful -- but an
    origin carrying a path, a query or userinfo is worse: it is a string a caller
    controls being used as the identity of an endpoint.
    """
    resolver = _CountingResolver()
    with pytest.raises(CredentialInvalidError):
        _cache(resolver).credential_for(CredentialReference("core.default"), origin)
    assert resolver.calls == []


@pytest.mark.parametrize("origin", [None, 7, b"https://core.example:443"])
def test_an_origin_that_is_not_a_string_is_refused_as_one(origin: object) -> None:
    with pytest.raises(CredentialInvalidError):
        _cache(_CountingResolver()).credential_for(
            CredentialReference("core.default"),
            origin,  # type: ignore[arg-type]
        )


def test_a_reference_that_is_not_a_reference_never_reaches_the_resolver() -> None:
    resolver = _CountingResolver()
    with pytest.raises(CredentialInvalidError):
        _cache(resolver).credential_for("core.default", ORIGIN)  # type: ignore[arg-type]
    assert resolver.calls == []


def test_the_seam_opens_nothing_and_so_can_be_no_credential_source() -> None:
    """The one thing this packet must not have added, asserted on the source.

    ``test_package_isolation.py`` already pins this module's imports to
    ``re``, ``threading``, ``time`` and the typing machinery, so there is no
    ``os``, ``pathlib``, ``socket`` or ``subprocess`` to reach a store *with*.
    What that leaves is the builtins, and ``open`` is the one that would turn
    this into a credential source without needing an import at all -- a file
    beside the descriptor, a token in the state directory. Checked on the parsed
    tree rather than by grepping the text, so the prose in the docstrings that
    describes what is absent does not match itself.
    """
    from omnivia_core_client import credentials

    tree = ast.parse(inspect.getsource(credentials))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called.isdisjoint({"open", "input", "__import__", "eval", "exec", "compile"})
