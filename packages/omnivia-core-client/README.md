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
Core's canonicalizer is frozen (ADR-039) and is not touched: `2^60` still has the
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

**No concrete transport ships in this package.**

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

This is candidate-level discovery through an injected transport. It does not ship a
socket, named-pipe, or HTTP transport and does not claim integrated discovery over a
real local endpoint.

### `errors` — the typed failures

`ClientError` and, under it, `ProtocolError`, `TransportError`,
`CompatibilityError`, `DeadlineExceededError`, and `OperationCancelledError`.

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

- **integrated discovery over a real local endpoint** — this packet verifies through
  an injected transport because no concrete Client local transport ships yet;
- **local socket transport** — Unix domain socket or Windows named pipe;
- **HTTP transport**;
- **retry, backoff, or idempotent replay**;
- **managed service startup** — launching or supervising a service;
- **the high-level client** — the object that would put the above together and
  execute an operation end to end.

Each arrives in its own packet and will satisfy the contracts above rather than
change them.
