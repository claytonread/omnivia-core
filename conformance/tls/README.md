# Hosted TLS conformance — how to run it, and against what

V06-4 P2b sub-lane B. A GO here is what closes V06-4. A GO on sub-lane A is not,
and the closeout must not read as though it is.

## What a pass means

Owner resolution 005 **R005-04** governs the claim, and the closeout must state
it in substance:

> V06-4 GO confirms the positive and negative conformance of the hosted TLS HTTP
> adapter. No production console path, credential resolver or serving lifecycle
> invokes the adapter. OmniVia Core does not yet serve HTTP as a production
> capability.

More precisely, per packet §10a.4: the run proves **the chain and hostname
presented by Core, as judged by an independent client**. It does not claim that
Core verifies client certificates — Core has no code that verifies one, and
mutual TLS is out of scope. The listener asks for no client certificate.

`test_the_standard_service_still_refuses_to_serve_http` is the regression that
keeps this honest rather than merely written down: `--http-endpoint` still
defaults to `None`, and every bind through the console script still exits 2.

## Topology

Fixed by packet §10a.3 and not re-openable here.

```
  GitHub Actions runner                     the conformance host
  ┌────────────────────────┐                ┌──────────────────────────────┐
  │ this suite (ephemeral) │ ── TLS ──────► │ HttpListener, TLS 1.2 floor  │
  │ public trust store     │   inbound      │ private key, never leaves    │
  └────────────────────────┘                └──────────────────────────────┘
```

The private key reaches the host and **only** the host. This side receives the
hostname, a temporary bearer credential, and the fingerprint of the chain the
host was configured with.

## Running it

Normally: the **Core TLS conformance** workflow, `workflow_dispatch` only, using
the `tls-conformance` environment. Locally, against an already-provisioned host:

```bash
export OMNIVIA_TLS_CONFORMANCE_HOST=conformance.example.net
export OMNIVIA_TLS_CONFORMANCE_PORT=8443
export OMNIVIA_TLS_CONFORMANCE_BEARER=...          # temporary, revocable
export OMNIVIA_TLS_CONFORMANCE_OPERATION=core.health
export OMNIVIA_TLS_CONFORMANCE_PURPOSE=conformance
export OMNIVIA_TLS_CONFORMANCE_CHAIN_SHA256=...    # sha256 of the leaf DER
export OMNIVIA_TLS_CONFORMANCE_EVIDENCE=/tmp/tls-conformance-evidence.json

python -m pytest conformance/tls -q -rs
```

Every variable is required. A missing one raises `HostConfigurationMissing`
naming it; nothing skips.

| Variable | What it is |
| --- | --- |
| `..._HOST` | the stable DNS name the listener is reachable at |
| `..._PORT` | the one open TCP port |
| `..._BEARER` | a temporary application credential the host's resolver accepts |
| `..._OPERATION` | one operation that credential's session is allowed to invoke |
| `..._PURPOSE` | a purpose that session holds |
| `..._CHAIN_SHA256` | SHA-256 of the DER of the leaf the listener was configured with |
| `..._EVIDENCE` | absolute path for the evidence artifact, **outside the work tree** |

`..._CHAIN_SHA256` is the anti-proxy proof (§6.2 item 3). Produce it on the host:

```bash
openssl x509 -in <the configured chain> -outform DER | shasum -a 256
```

## What the host must provide

Packet §6.6, plus the eight properties of §10a.5 and the provisioning spec:

1. A stable DNS name resolving, from GitHub Actions runners, to the listener.
2. A chain issued for that name **by an authority already in the public trust
   store**, with the private key deliverable only to the host.
3. A short validity window, with revocation and name retirement after closeout.
4. A synthetic workspace and a temporary bearer credential.
5. **No TLS-terminating intermediary** between this client and the listener.
6. `openssl` on the runner — already present on `ubuntu-latest`.

The listener must be brought up by an approved embedder constructing
`HttpListener` directly with a harness-owned credential resolver, exactly as the
merged loopback suites do. **Not** by `omnivia-core-service --http-endpoint`,
which supplies no resolver and exits 2 by design — see the divergence below.

## What no host buys you

Nothing here can pass without one, deliberately. `host` is session-scoped and
autouse, so the locally generated negative cases cannot be green on a runner
that never reached a listener. An absent host produces 28 errors and **zero
skips**, and the evidence artifact records the counts.

That is the whole resolution of the zero-skip rule: `conformance/` sits outside
`testpaths`, outside every path `core-acceptance.yml` names, and outside
`phase2-platform.yml`, so nothing invokes this suite except the dispatch-only
workflow. "Not run" and "passed" stay different states without a marker.
`scripts/check-test-collection.py` exempts the tree from its bare-run
completeness check, and `tests/test_core_acceptance_workflow.py` is what stops
that exemption from being a hole.

## Where the anchors come from

- **`public_client`** — `ssl.create_default_context()`, public trust store,
  `check_hostname` on, `CERT_REQUIRED`. The only context ever pointed at the
  host.
- **`local_client`** — the same constructor anchored on the generated CA. Not
  laxer; differently anchored, and it never touches the host. It exists because
  an expired chain under an *untrusted* issuer reports `verify_code` 20 rather
  than 10, so a case asserting 20 there would keep passing after expiry checking
  broke.

Generated chains are made by the `openssl` CLI into `tmp_path_factory` (§10a.2)
and presented through paired `ssl.MemoryBIO`s — no port is bound, nothing is
left behind, and they can never be reached from outside the process. They use
`generated.conformance.invalid`, so they are unusable against anything real.

Attribution, all verified: expired → 10, untrusted issuer → 20, self-signed →
18, hostname mismatch → 62, downgrade → a peer protocol-version alert.

## Divergences found while building this — for PM

Recorded rather than resolved at lane level.

1. **The provisioning spec's diagram shows `omnivia-core-service
   --http-endpoint https://…` on the host.** That invocation cannot serve: the
   console script supplies no credential resolver and exits 2, which packet §4
   requires and §6.4 item 7 asserts as a regression. The host must start the
   listener through an approved embedder instead. The diagram is illustrative;
   the packet is normative.

2. **The provisioning spec allows a self-signed host certificate "only if the
   client is given its CA explicitly".** Packet §6.2 item 2 forbids an anchor
   derived from the chain under test, and §6.6 item 2 requires an authority the
   public trust store already contains. This suite implements the packet: public
   trust store, no override. A self-signed host chain is a provisioning defect,
   not a client option.

3. **"Assert on the dispatcher, not the status code" (§6.4 items 5–6) is not
   reachable from an inbound client.** The merged loopback suites use a
   recording dispatcher in-process; a network peer sees only the wire. The
   substitute here is attributable rather than a bare status check: the
   unauthenticated request carries a body that is *independently* unacceptable
   (wrong media type, non-canonical JSON), and the answer must still be `401`.
   A 401 that beats the 415 and the 400 is a refusal taken before the body was
   read — which is the structural property the loopback suites prove directly.

4. **Certificate-load failure on the host is not re-provable from here.** §6.4
   item 8 extends redaction to it; sub-lane A proves it hermetically. This suite
   covers the reachable surfaces: handshake failures, endpoint-parse refusals,
   and every response.

5. **The Decision 4 regression runs on the runner, not the host.** An inbound
   client can start no process on the conformance host. It is the same artifact
   at the same commit, and the refusal is a property of the artifact.
