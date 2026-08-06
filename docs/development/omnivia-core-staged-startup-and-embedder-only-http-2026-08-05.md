# OmniVia Core Owner Decision Record: staged `coordinated_startup`, embedder-only HTTP

- **Date:** 2026-08-05
- **Core baseline:** `4816b651af874ab03b49a5bbf8ab986f1dab993c`
- **Decision authority:** Product and Architecture Owner
- **Source:** `OMNIVIA-CORE-OWNER-DECISIONS-RESOLUTION-001`, sections 6 and 7 —
  `omnivia-pm`:
  `docs/tasks/2026-08-05-omnivia-core-owner-decision-resolution-handoff.md`
- **Status:** Decided. This document records the decisions; it does not reopen them.

## Why this document exists

Two pieces of merged Core code have no production caller, and in both cases the
absence is deliberate. Without a record, the next reader has two plausible and
wrong conclusions available:

- that `service/bootstrap.py::coordinated_startup` is unreferenced runtime code
  and may be deleted;
- that Core v0.6 serves an HTTP boundary, because the loopback HTTP trust
  boundary was merged.

The owner has decided both, and the code sites point here rather than restating
the decisions, so there is one record to keep current instead of three.

This repository has no changelog and no release-notes tree — nothing under
`docs/`, no root `CHANGELOG.md`, and no release-drafting workflow under
`.github/workflows/`. Section 7.2 below is addressed to "documentation and
release records", so until a release record exists this document is the record
for the v0.6 HTTP declaration. If one is introduced later, it should carry the
same declaration and cite this file rather than paraphrase it.

Repository qualification: architecture decision records cited from Core are
qualified by repository, per the `omnivia-pm` ADR README. `PM ADR-037` is
`omnivia-pm`: `docs/adr/ADR-037-core-service-ownership-bootstrap-and-workspace-fencing.md`.

---

## 1. `coordinated_startup` is staged, not orphaned

Site: `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/bootstrap.py`

### 1.1 Decision

`coordinated_startup` is **staged, and retained as a platform-neutral runtime
bootstrap API**. It implements the `PM ADR-037` startup sequence — discover,
acquire the bootstrap mutex, re-discover, spawn when required, release the
bootstrap mutex. Its lack of a production caller is resolved by naming and
sequencing the intended consumers, not by treating it as automatically orphaned.

### 1.2 Named consumers, in this order

1. **`omnivia-core-cli` managed-local start**, as the first Core-owned
   production caller.
2. **Official MCP managed stdio mode**, when MCP is permitted to ensure that its
   local Core service is running.
3. **`omnivia-platform` App Shell Host or Core Connectivity adapter**, when
   Desktop connects to or launches a same-device Core.
4. Future installers, status-menu helpers or service managers that use the same
   runtime contract.

### 1.3 Boundary rules

The helper must remain free of:

- Electron dependencies;
- Desktop user-interface concerns;
- Platform account or licensing logic;
- Module entitlement logic;
- application-specific workspace operations.

Its responsibilities are limited to:

- service discovery;
- short-lived bootstrap coordination;
- version and compatibility checking;
- process launch;
- connection handoff;
- safe cleanup after failed managed startup.

It must never acquire or act as the authoritative workspace service lease
holder. The Core Service remains the only workspace lease owner.

### 1.4 Delivery target and removal trigger

The target is milestone-based rather than calendar-based, because the available
handoff material supplies no sprint calendar from which a defensible date could
be derived. Both of the following are normative and are quoted verbatim.

Delivery target:

> The first production caller must land before V06-3 closes, with
> `omnivia-core-cli` as the required initial caller.

Removal trigger:

> If no approved production caller is implemented by the V06-3 closeout,
> `coordinated_startup` and the launcher-facing portion of `is_compatible` must
> be reconsidered for relocation, conversion into a deliberately retained
> reference implementation, or removal. They must not remain indefinitely as
> speculative runtime code.

The launcher-facing portion of `is_compatible` is in
`packages/omnivia-core-runtime/src/omnivia_core_runtime/ownership/discovery.py`.

---

## 2. HTTP is embedder-only for v0.6

Sites:
`packages/omnivia-core-runtime/src/omnivia_core_runtime/service/http_transport.py`
and `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/main.py`

### 2.1 Decision

The HTTP adapter is **embedder-only and intentionally unreachable from the
standard Core service for v0.6**. The merged work is an adapter seam, not a
currently served Core boundary.

### 2.2 Normative declaration

Documentation and release records state, verbatim:

> OmniVia Core v0.6 contains a loopback-safe HTTP adapter that may be wired by
> an approved embedder or conformance harness. The standard Core service and CLI
> do not expose an HTTP listener because no approved credential source or
> bearer-session resolver has yet been defined. This does not remove
> authenticated HTTP from the target architecture.

### 2.3 What the code does today

The declaration above is checkable against three facts in this tree:

- `--http-endpoint` defaults to `None`, so nothing is bound unless it is asked
  for (`service/main.py`);
- the console-script entry point calls `main()` with no `resolve_credential`, so
  an embedder is the only caller that can supply one;
- `main()` therefore exits `2` for every run that would actually serve an HTTP
  bind reached through the console script — either the endpoint is not an
  accepted loopback endpoint, or there is no trusted credential resolver — and
  `LoopbackHttpServer` refuses construction without a resolver as a second lock.

> **Correction, 2026-08-06.** The third bullet read "for every HTTP bind reached
> through the console script", with no qualifier. That absolute was wrong, and
> the same absolute had been copied into the `--http-endpoint` help text, the
> `http_transport` module docstring and `main()`'s docstring; all four now carry
> the qualifier.
>
> `--check-only` serves nothing, so `main()` never calls `_http_bind_to_serve`
> and never parses `--http-endpoint` at all. Measured on a ready workspace,
> `--check-only --http-endpoint <value>` exits `0` with empty stderr for
> `http://0.0.0.0:8080`, `https://127.0.0.1:8080`, `http://198.51.100.7:8080`,
> `http://[::1` and unparseable text alike — so the endpoint rule that section
> 2.3 relies on is not applied in that mode either. `--endpoint` has always been
> ignored there in the same way; the silence was undocumented, and both flags'
> help texts now state it.
>
> The decision in 2.1 and the normative declaration in 2.2 are unaffected and
> are not reopened: nothing is served under `--check-only` either, which is what
> they govern. What was wrong is the absolute that was stated alongside them,
> not the declaration itself.

### 2.4 Future architectural role

Authenticated HTTP remains necessary for:

- Core hosted on a NAS or LAN server;
- another computer connecting to a local Core host;
- OmniVia Cloud;
- hybrid Desktop-to-remote-Core operation;
- future organisation deployments.

Narrowing the transport gate to loopback permanently would conflict with that
topology, so the loopback restriction is a v0.6 property of the *standard
service*, not a decision about the target architecture.

### 2.5 Credential-source boundary

The credential source should eventually come from the host boundary:

- trusted local configuration for local service clients;
- Platform credential brokerage for Desktop;
- Cloud identity and tenant services for hosted Core;
- an approved administrative configuration for standalone network Core.

The transport adapter consumes a validated credential resolver or
`AuthenticatedSession`. It must not invent an account database, token issuer or
credential store inside the Core HTTP lane.

### 2.6 Revisit trigger

Production HTTP wiring should be reconsidered only after:

1. the V06-2 session policy is implemented and verified;
2. an approved credential source is assigned;
3. bearer-session construction and revocation semantics are defined;
4. the TLS conformance lane has passed;
5. negative tests prove that client claims cannot expand principal, workspace,
   purpose or capability grants;
6. operational secret storage and redaction are implemented.

Decision 1 of the same handoff provisions a TLS transport conformance host. That
environment certifies the non-loopback adapter; it does not make the standard
Core service HTTP-reachable, and the two decisions are not in tension.
