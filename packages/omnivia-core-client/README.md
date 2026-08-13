# omnivia-core-client

`omnivia-core-client` is the shared client protocol foundation in the OmniVia
Core package topology: the pieces every OmniVia client needs in common, before
any of them can talk to a running service.

It depends on the public `omnivia-core` contracts and on nothing else — not the
runtime, not the CLI, not the MCP surface, and no third-party library.

## Dependency direction

```text
omnivia-core-client  -->  omnivia-core
```

- `omnivia-core-client` depends on `omnivia-core`.
- `omnivia-core` must never depend on or import `omnivia_core_client`.
- `omnivia-core-client` must never depend on or import `omnivia_core_runtime`,
  `omnivia_core_cli`, or `omnivia_core_mcp`.

## What this package provides today

### `framing` — the frozen OVC1 frame

Pure encode and decode of one frame:

```text
"OVC1"                             4 bytes, ASCII magic (4f564331)
JSON byte length                   4 bytes, unsigned big-endian uint32
canonical UTF-8 JSON               exactly that many bytes, nothing after
```

- The JSON payload is at most **4 MiB (4194304 bytes), inclusive**.
- A frame's root is always a **JSON object**.
- Decoding re-encodes what it parsed and requires the bytes to match exactly,
  so a non-canonical spelling of a valid value is rejected rather than silently
  re-emitted differently.
- Rejected on decode: a wrong magic, a zero or over-large declared length, a
  truncated frame, trailing bytes, invalid UTF-8, invalid JSON, a scalar or
  array root, a value outside the admitted JSON domain, and non-canonical JSON
  bytes.
- Rejected on encode: a payload outside the admitted JSON domain, one over the
  size bound, and — the closure rule below — one whose canonical bytes the
  decode path would not admit.

This module is **pure**. It performs no socket or pipe I/O, holds no state
between calls, and provides no connection pooling, no multiplexing, no
streaming or chunked payloads, and no server-initiated push.

#### Canonical JSON is RFC 8785, and only that

The canonical form is **RFC 8785 (the JSON Canonicalization Scheme) over the
admitted I-JSON value domain**, produced by Core's public implementation,
`omnivia_core.contracts.v1.canonical_json.canonical_bytes`, together with the
admission policy stated in that module. This package defines no second,
wire-specific canonical form; there is one algorithm here and it is the same one
Core applies when content-addressing a Context Pack artifact.

The rules a second implementation must match:

| rule | value |
| --- | --- |
| encoding | UTF-8 |
| member order | by **UTF-16 code unit** of the member name, not by code point |
| numbers | ECMAScript `Number::toString(x, 10)` |
| strings | seven short escapes, `\u00xx` lowercase for the rest of C0, everything else literal |
| value domain | I-JSON |
| maximum nesting | 64 levels (`MAXIMUM_JSON_NESTING_DEPTH`, read from Core) |

The consequences worth knowing before writing an encoder:

- `1.0` and `-0.0` are spelled `1` and `0`; `1e20` is written out positionally
  as `100000000000000000000`; the exponential form takes over at `1e+21` and at
  `1e-7`. A `repr`- or `printf`-based encoder gets all four wrong.
- A member name containing a supplementary character sorts by its high
  surrogate, so `"\U0001F600"` comes **before** `"דּ"`. Sorting by code
  point gives the opposite order and is wrong.
- A value outside I-JSON is **refused, never coerced**: a non-string member name
  is a rejection rather than a member renamed to its string form, a duplicated
  member name is a rejection rather than a last-one-wins merge, and a number
  with no exact binary64 value is a rejection rather than a rounded number the
  sender never wrote.

#### Encoding is closed under decoding

**An encoder returns a frame only when the canonical bytes it produced are
themselves accepted as canonical by the decode path.** `encode_frame()` and
`canonical_json_bytes()` put their own output through the decoder's admission —
parse, admit, re-canonicalize, compare — and raise `ProtocolError` rather than
return bytes that would fail on arrival. Every frame a caller receives from this
package decodes; there is no third outcome.

The rule exists because two separately conformant halves do not compose to a
total function on their own. The admitted domain is a rule about a *value* — an
integer is admitted when converting it to binary64 and back is exact — while
what travels is the decimal token that value renders to, and that token has to
clear the same rule in its own right. `1152921504606846976` (2^60) is exact,
renders as `1152921504606847000`, and that decimal is not exact, so those bytes
cannot be decoded. A sender needing an integer identity that large must carry it
as a string.

**This is a transport rule and it changes nothing about the canonical form.**
Core's canonicalizer is frozen (PM ADR-039) and is not touched: `2^60` still has the
same RFC 8785 canonical bytes, and code content-addressing a document rather than
sending one should call `canonical_bytes` directly and will see no difference.
Nor is the boundary a JavaScript-safe-integer range — the admission rule remains
exactness, not magnitude. `18014398509481984` (2^54) is above 2^53 and still
frames, because its canonical token is the same digits it started as. Both
directions are pinned as vectors.

The frozen format is pinned by a checked-in, language-neutral vector manifest at
`tests/fixtures/ovc1-v1.json` (`format: omnivia.ovc1.v1`), in four parts:

- `vectors` — the application request, success and error envelopes and the
  health, readiness and discovery probes, with exact canonical-JSON and
  complete-frame hex;
- `canonicalization_vectors` — the edge cases above, each one a place where a
  plausible implementation disagrees with RFC 8785;
- `rejected_vectors` — documents that must be refused on decode, each labelled
  with a category from `rejection_reasons`;
- `encoder_rejected_vectors` — payloads an encoder must refuse to send under the
  closure rule, each labelled from `encoder_rejection_reasons` and recording the
  canonical bytes a non-conforming encoder would emit instead.

The test suite recomputes every accepted vector rather than trusting the stored
hex, decodes every rejected one to confirm it is refused, and encodes every
encoder-rejected one to confirm no frame comes back — while asserting that Core's
canonicalizer still produces the recorded bytes for it, so the refusal is
demonstrably the transport's and not a narrowed canonical form. A second
implementation in another language can be held to the same bytes and the same
two boundaries.

### `deadline` — one deadline per call, and cancellation

- `Deadline` owns an injected monotonic clock and one absolute end computed at
  construction. It answers `remaining_seconds()`, `remaining_ms()`, `expired`,
  and `assert_not_expired()` against that same end. It is frozen and has no
  `reset` or `extend`: a retry, a reconnect, or a second frame inside one call
  reads what is *left*, never a fresh budget.
- **A relative budget is a wire value and is bounded like one.** `after()` and
  `after_ms()` accept only a finite, non-negative duration within the contract's
  `DurationMs` range, `[0, 86400000]` ms — `MAXIMUM_DURATION_MS`, or
  `MAXIMUM_TIMEOUT_SECONDS` (86400.0) in seconds. A larger one is refused rather
  than clamped. `remaining_ms()` is capped at `MAXIMUM_DURATION_MS`, so every
  value it returns is a valid `DurationMs`; capping reports *less* time than
  there is, never more.
- **Every public failure is intentional.** A wrong *kind* of value raises
  `TypeError` (including `bool`, which Python would otherwise admit as an
  `int`), and an impossible *amount* raises `ValueError`. Neither is ever a
  `math` or arithmetic error escaping from mid-calculation, and no diagnostic
  renders the offending value — an integer of a few thousand digits cannot be
  converted to a string at all, and formatting one would replace the diagnostic
  with an unrelated failure.
- `CancellationToken` carries thread-safe, one-way cancellation state plus the
  callbacks a transport registers to interrupt a blocked read. Callbacks run in
  registration order, exactly once, and a callback registered after cancellation
  runs inline. A callback's `Exception` is contained — it never stops the other
  callbacks and never escapes `cancel()` — and is readable from
  `callback_failures`.
- `raise_if_cancelled()` is the pre-send check: a call cancelled before it
  started is never put on the wire.

### `transport` — the transport contract

`ClientTransport` is a `typing.Protocol` (runtime-checkable) with two calls:

- `call(request: RequestEnvelope, *, deadline, cancellation) -> ResponseEnvelope`
- `probe(request: ServiceProbeRequest, *, deadline, cancellation) -> ServiceProbeResult`

Both take the whole-call `Deadline` rather than a relative timeout, so an
implementation derives its remaining time at the moment it is about to wait.
`enforce_send_preconditions()` is the shared precondition every implementation
calls before writing its first byte: cancellation first, then the deadline.

Two concrete transports satisfy it, both in this package: `LocalIpcTransport`
over the installation-local `unix://` endpoint on POSIX or `pipe://` endpoint on
Windows, and `HttpTransport` over an authenticated HTTP v1 endpoint.

`LocalIpcTransport` carries the same raw OVC1 byte stream over both platform
mechanisms. The Windows client uses overlapped `WaitNamedPipeW`/`CreateFileW`,
`ReadFile` and `WriteFile` calls with one absolute call deadline; it asks for
duplex access, which preserves the Runtime listener's creating-user boundary,
and retries only the unavoidable wait/create busy race. Native APIs are loaded
lazily, so importing the package remains safe on POSIX. Neither local mechanism
claims cryptographic peer identity; it establishes only that this process could
open the operating-system-protected endpoint.

### `credentials` — a name for a credential, bound to one endpoint origin

**This package holds no credential source and adds none.** There is no token
format, issuer, store, broker, keychain or account database here, and nothing
reads an environment variable, an argument vector, a configuration file or a
file beside anything. Resolution is a callable the host injects.

- `CredentialReference` is a **name, not a secret** — the thing a descriptor, a
  configuration file or a log may legitimately carry. The grammar is an
  allowlist: 1–256 characters, opening with a letter or digit and continuing
  with letters, digits, `.`, `_` or `-`. That alone refuses control characters,
  whitespace, `@` and `:` in the userinfo position, `?`/`&`/`=`/`#` from a
  query, a `%` escape, and `/` or `\` from a path. Two further rules close what
  the character set leaves open: no `..` anywhere, and nothing shaped like a
  compact JWS (`ey…` plus two dot-separated segments) — a token pasted where the
  *name* of a token belongs.
- `Credential` is the **secret**, and it is opaque by construction. `repr()` and
  `str()` are both a fixed `<credential redacted>`, so an f-string, a `%r`, a
  `pprint` and a structured log are all safe by default; `reveal()` is the only
  way to the material and is named so that reading it shows up in a diff. The
  accepted secret is 1–4096 visible ASCII characters with no space, which is
  what an `Authorization` field value may carry — so `\r\n` in a secret is a
  refusal here rather than header injection at a socket.
- `CredentialCache` is the only caller of the resolver, keyed by
  **`(origin, reference)`**. The origin must be a normalized origin — lowercase
  scheme and host, explicit port, IPv6 in brackets. **A credential resolved for
  one origin is never presented to another**, and that is a property of the key
  rather than a rule a caller has to remember. Entries expire after
  `ttl_seconds` (60 s by default; `0` caches nothing) and `clear()` drops them
  all, which is what a process calls at shutdown.

The resolver is handed the reference and the origin, and answers with a
`Credential` or `None`. Four typed outcomes and no fifth, all under
`CredentialError`:

| outcome | raised when |
| --- | --- |
| `CredentialMissingError` | the resolver answered `None` |
| `CredentialDeniedError` | the resolver raised `CredentialDeniedError` — the one it raises on purpose |
| `CredentialUnavailableError` | the resolver raised anything else |
| `CredentialInvalidError` | a reference outside the grammar, or an answer that is not a usable `Credential` |

**Nothing a resolver does escapes the seam.** Every message above is a fixed
sentence written in `errors.py`, built from no reference, no origin, no store
location and no resolver diagnostic, and raised *after* the handler ends — so
`args`, `__cause__` and `__context__` are all clean. The tests assert the
absence of both links against a deliberately hostile resolver, not merely that
the default traceback rendering hides them.

### `http_transport` — the authenticated HTTP v1 transport

`HttpTransport` dials the two routes the runtime's HTTP adapter serves,
`POST /v1/application` and `POST /v1/probe`, and satisfies `ClientTransport`.

- **The endpoint** is scheme, host and port and nothing else.
  `parse_http_endpoint()` refuses anything in the `userinfo` position, a query,
  a fragment and a path past `/` — a credential must never travel in a URL, and
  the way to guarantee that is to refuse a URL with a place to put one. Parsing
  is `urllib.parse.urlsplit`, not a hand-written splitter. Scheme, host and
  effective port are normalized, including bracketed IPv6, so one endpoint has
  exactly one origin.
- **Cleartext `http://` reaches a loopback IP literal and nothing else**,
  decided by `ipaddress`. `localhost` is refused rather than resolved: a policy
  an `/etc/hosts` line can move is not a policy. Every other endpoint is
  `https://` with `ssl.create_default_context()` — certificate verification and
  hostname checking on. **There is no argument, field, environment read or code
  path that turns either off.**
- **The credential** is resolved per call against this endpoint's normalized
  origin and reaches exactly one place: the `Authorization: Bearer` header of
  the request being sent. Not the URL, not a field of the transport, not a
  diagnostic, not a log — this module has no logger — and not any exception it
  raises. It is sent on every `/v1/application` request and on the
  `service.discover` probe, which the server authenticates; `service.health` and
  `service.readiness` are the accepted unauthenticated pair and carry none.
- **The exchange is unary**: one connection per call, `Connection: close`,
  `Content-Type: application/json`, a canonical-JSON body, and no retry. **No
  OVC1 frame travels over HTTP** — the body is bare canonical JSON, which is what
  the server reads. The eight-byte header is put in front of a *received* body
  locally, purely to reach `decode_frame()`, so one canonical-JSON admission
  policy governs both transports in both directions. Redirects are not followed,
  and not because they are checked for: `http.client` follows nothing.
- **HTTP 200 is the only status that carries an answer**, and both answers it can
  carry — a success envelope and a typed application *error* envelope — are
  returned normally. Any other status is a `TransportError` naming the number
  and nothing else; it is deliberately not translated into a credential outcome,
  because 401 and 403 are the server's own gate and a client that guessed would
  be publishing a security decision it does not hold.
- The whole-call deadline is enforced before the first byte and re-checked
  before every blocking wait, so a slow connect cannot buy the response read a
  fresh budget. Timeouts are `DeadlineExceededError`, unreachable peers and
  mid-response drops are `TransportError`, and a malformed status line, media
  type, `Content-Length` or body is a `ProtocolError`.

`http`, `ssl`, `urllib` and `ipaddress` are pinned by
`tests/test_package_isolation.py` to this one module by name, exactly as
`socket` is pinned to `local_ipc`. A third-party HTTP client stays forbidden
everywhere: the point was never that this package cannot speak HTTP, it is that
it has exactly one dependency.

### `compatibility` — what this build can talk to

- The API contract version is negotiated: `select_api_version()` returns the
  highest version both inclusive windows admit. The client window is derived
  from `omnivia_core.contracts.v1.CONTRACT_VERSION`; no API version is
  hard-coded here.
- The OVC1 protocol version is **not** negotiated. This build implements
  exactly `1.0`, and any other major or minor is refused.
- A descriptor shape version is readable at any minor of the same major
  (additive), and refused at another major. Accepting a newer minor says
  nothing about its extra fields: unknown optional fields remain the decoder's
  concern, and the public DTO decoding is not weakened.
- `negotiate_endpoint()` answers all three from an accepted
  `ServiceEndpointDescriptor`.

Malformed or reversed windows, windows with no overlap, and unknown majors all
fail closed with a `CompatibilityError` — and so does a malformed *direct* value.
Every function here is an entry point a hand-built or `dataclasses.replace`d
descriptor can reach, so a version or window bound that is not a string is
checked before the public contract helpers see it. No raw `TypeError` from a
regular-expression call, and no contract-layer exception, escapes to a caller
who wrote `except ClientError`.

### `discovery` — installation-local descriptor discovery

`discover_endpoint()` accepts a trusted installation-state root, a public
`WorkspaceId`, an injected `ClientTransport`, and the whole-call `Deadline` and
optional `CancellationToken`. It derives exactly one path:

```text
<installation-state>/runtime/<workspace_id>/service.json
```

There is no caller-selected descriptor path, glob, environment lookup, implicit
home fallback, or workspace-storage fallback. An absent path is a transient
publication state and returns `None` without contacting the transport.

A present descriptor is coordination data, never authority. Before decoding,
discovery:

- rejects a symlinked descriptor or derived parent and any non-regular descriptor
  — a FIFO or socket published at the descriptor path is refused on the spot,
  never waited on for a writer;
- requires the descriptor and derived parents to be owned by the current user and
  inaccessible to group/other on POSIX;
- applies the owner-equivalent native owner/DACL check on Windows, admitting access
  only for the current owner, LocalSystem, and built-in Administrators;
- opens through no-follow directory/file handles on POSIX and compares native file
  identities before and after the read, so replacement races fail closed; and
- checks the **64 KiB (65536-byte) inclusive** bound before reading and stops the
  read at one byte beyond the bound, so a concurrent grow is bounded too.

UTF-8, JSON, duplicate-member, non-finite-number, public DTO, and endpoint-policy
failures become fixed, payload-free `ProtocolError`s with no nested parser
exception. The public `decode_service_endpoint_descriptor()` remains the endpoint
*publication* policy authority, and discovery admits no URI it refused. What
discovery adds on top is a locality rule, not a second URI grammar: that shared
policy answers what is safe to publish before authentication, so it admits remote
HTTP endpoints and both platforms' local IPC schemes, while installation-local
discovery connects only to this platform's local IPC endpoint — `unix://` on
POSIX, `pipe://` on Windows. The guard runs after the public decoder, so it
only narrows what that decoder already admitted; it is a prefix check, not a
second grammar.
Anything else is a `TransportError` raised before the transport is touched.

The accepted descriptor is passed to `negotiate_endpoint()` unchanged, so descriptor
shape, exact OVC1 protocol, and API overlap keep their accepted order and failure
types. Before returning the immutable `DiscoveredEndpoint`, the injected transport
must answer `service.discover` with a descriptor whose workspace and service-instance
identities exactly match the file. Optional pid/start/boot evidence is corroboration
only: it never overrides a live identity mismatch and is not process authority.
Cancellation is checked before the probe is sent, and the same deadline/token are
passed unchanged to the transport.

This is candidate-level discovery through an injected transport. `discovery` itself
ships no transport: it is handed one, and the two this package provides —
`LocalIpcTransport` and `HttpTransport` — are constructed by the caller, not by it.
It does not claim integrated discovery over a real local endpoint by itself;
the caller supplies `LocalIpcTransport`, which now covers the accepted local
mechanism on both POSIX and Windows.

### `errors` — the typed failures

`ClientError` and, under it, `ProtocolError`, `TransportError`,
`CompatibilityError`, `DeadlineExceededError`, `OperationCancelledError`, and
`CredentialError` with its four outcomes above.

Diagnostics are built from structural facts only — byte counts, offsets, JSON
value kinds, version strings, field names — and never from payload content, so
an exception can be logged without leaking a credential that was in flight.

That rule extends to the exception *chain*, and is enforced rather than assumed:
a `ProtocolError` raised for a rejected frame or payload has **neither a
`__cause__` nor a `__context__`**. Both the standard library's parser and Core's
canonicalizer name the offending value in their own diagnostics — correctly, for
a developer canonicalizing a document they own — so those exceptions are handled
where they occur and a prepared, payload-free sentence is raised outside the
handler. The tests assert the absence of both links, not merely that the default
traceback rendering hides them.

## Not implemented yet

None of the following exists in this package, and no caller may assume it:

- **retry, backoff, or idempotent replay**;
- **managed service startup** — launching or supervising a service;
- **the high-level client** — the object that would put the above together and
  execute an operation end to end.

Each arrives in its own packet and will satisfy the contracts above rather than
change them.
