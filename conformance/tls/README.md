# Hosted TLS conformance — domain-free, and what a GO actually claims

A GO here is what closes V06-4. It is produced by one workflow, `Core TLS
conformance`, which is `workflow_dispatch` only and runs the 28 cases in
`test_tls_conformance.py` against a listener started by `host.py` on a
provisioned DigitalOcean droplet.

## What a pass means

> V06-4 GO confirms the positive and negative conformance of the hosted TLS HTTP
> adapter. No production console path, credential resolver or serving lifecycle
> invokes the adapter. OmniVia Core does not yet serve HTTP as a production
> capability.

More precisely: the run proves **the chain and address presented by Core, as
judged by an independent client, on a named candidate commit**. It does not
claim that Core verifies client certificates — Core has no code that verifies
one, and mutual TLS is out of scope. The listener asks for no client
certificate.

`test_the_standard_service_still_refuses_to_serve_http` is the regression that
keeps this honest rather than merely written down: `--http-endpoint` still
defaults to `None`, and every bind through the console script still exits 2.
`host.py` is an approved embedder that never touches that entry point, never
constructs `Dispatcher`, and never calls `build_service_registry`.

## Topology, and why there is no domain

```
  GitHub Actions runner                    the conformance droplet
  ┌────────────────────────┐               ┌───────────────────────────────┐
  │ this suite (ephemeral) │ ── TLS ─────► │ host.py → HttpListener        │
  │ public trust store     │  inbound,     │ TLS 1.2 floor, iPAddress SAN  │
  │ no key, no CA bundle   │  IPv4 literal │ private key, never leaves      │
  └────────────────────────┘               └───────────────────────────────┘
```

No DNS name is registered, resolved or verified anywhere in this lane. The
certificate carries an `iPAddress` SAN for the droplet's public IPv4 and nothing
else, and the suite asserts exactly that — the address set is `[<the IPv4>]` and
the DNS set is empty. Two consequences follow and both are load-bearing:

* the wrong-peer refusal is OpenSSL's **64**, `IP_ADDRESS_MISMATCH`, not the 62
  a DNS-named lane would see. Asserting 62 here would pass only if the address
  checking under test never ran;
* nothing about this lane depends on a registrar, a zone, or a name that outlives
  the run. Retiring the host is releasing a droplet.

The private key reaches the droplet and **only** the droplet. The runner receives
the public IPv4, the port, the grant, a temporary bearer credential and the
fingerprint of the chain the host was configured with. It is given no CA bundle
derived from that chain and no private key — see *Where the anchors come from*.

## The certificate: a public IPv4, no domain

Let's Encrypt issues publicly-trusted certificates for a bare IPv4 address under
its **short-lived profile**, and Certbot has supported that since **5.4**. Two
constraints come with it and both are the CA's, not ours: an IP certificate is
only ever issued under the short-lived profile, and its validity window is days
rather than months. Confirm the profile name against Let's Encrypt's own
documentation before provisioning — it is theirs to rename.

The only two ACME challenge types available without a name are `http-01` (port
80) and `tls-alpn-01` (port 443). This lane uses `http-01`, which is why port 80
appears below at all — and why it is closed again before the listener starts.

`--webroot` is used rather than `--standalone`: Certbot writes the challenge
token into `<webroot-path>/.well-known/acme-challenge/` and something else has to
serve that directory over **cleartext HTTP on port 80** for the duration of the
validation. A conformance droplet runs no web server, so the temporary one below
is `python3 -m http.server`, started before Certbot and killed after it. It
serves the challenge directory and nothing else, and it is gone before the Core
TLS listener is started.

Run staging first. A rejected production order costs a rate-limit slot against an
address the CA will not issue for again quickly; the staging directory produces
an untrusted chain, which is exactly enough to prove the challenge and the
profile are right before it matters.

On the droplet, as root:

```bash
# 1. Only SSH and, temporarily, ACME. The Core TLS port stays shut for now.
ufw default deny incoming
ufw allow 22/tcp
ufw allow 80/tcp                 # ACME http-01 only
ufw enable

# 2. The webroot, and the throwaway server that publishes it on port 80.
mkdir -p /var/www/acme/.well-known/acme-challenge
python3 -m http.server 80 --bind 0.0.0.0 --directory /var/www/acme &
webroot_server=$!

# 3. Staging first. The chain this produces is untrusted and is thrown away; what
#    it proves is that the challenge is reachable and the profile is accepted.
certbot certonly --staging \
  --preferred-profile shortlived \
  --webroot --webroot-path /var/www/acme \
  --ip-address <public IPv4> \
  --agree-tos --email <operator> --non-interactive

# 4. Retire the staging lineage. Certbot names it after the identifier, so the
#    production order below would otherwise collide with it and --non-interactive
#    has no way to answer the prompt that follows.
certbot delete --cert-name "<public IPv4>" --non-interactive

# 5. The same order against production — the only difference is that --staging is
#    gone. This is the chain the listener presents.
certbot certonly \
  --preferred-profile shortlived \
  --webroot --webroot-path /var/www/acme \
  --ip-address <public IPv4> \
  --agree-tos --email <operator> --non-interactive

# 6. Close ACME before anything is served. From here until the host is retired
#    the direct Core TLS port is the only reachable port besides SSH.
kill "${webroot_server}"
ufw delete allow 80/tcp
ufw allow <core tls port>/tcp
ufw status verbose               # record this in the provisioning note
```

Certbot names the lineage after the identifier, so the material lands at:

```
/etc/letsencrypt/live/<public IPv4>/fullchain.pem   → --certificate-chain
/etc/letsencrypt/live/<public IPv4>/privkey.pem     → --private-key
```

Those two are what `host.py` is given below. `fullchain.pem` rather than
`cert.pem`: the runner anchors on the public trust store alone and is given no
bundle, so the listener has to present the intermediates itself.

Check the result before trusting it — a chain with a `DNS` SAN, or with more than
one address, fails the suite rather than passing quietly:

```bash
openssl x509 -in /etc/letsencrypt/live/<public IPv4>/fullchain.pem \
  -noout -text | grep -A1 'Subject Alternative Name'
# X509v3 Subject Alternative Name:
#     IP Address:<public IPv4>
```

The validity window is days, not months. Provision, run, and retire inside it;
a run that outlives the certificate is a run that reports expiry, correctly.

## Launching the host

`host.py` is in this tree on purpose. A launcher that lived only on the droplet
could not be part of the proof: it reads its checkout's **committed** commit and
tree **before it binds** and returns them through `conformance.attest`, and the
runner requires them to equal its own. That is what `candidate_identity_match`
records.

`tree_sha` is `HEAD^{tree}` — the tree of the last commit, not a reading of the
files on disk — so on its own it would be identical on a checkout somebody
edited. A clean checkout is therefore a **separately enforced precondition**, not
something the attested pair carries: `host.py` refuses to bind when
`git status --porcelain --untracked-files=all` reports anything, and the suite
refuses to report the runner's pair under the same rule. Both refusals name no
path and quote no status line. Launch from a fresh clone at the candidate commit,
and generate the workspace, bearer and certificate **outside** it — a marker file
or a key written into the checkout is an untracked change and the host will
refuse.

```bash
git clone <repo> && cd <repo> && git checkout <candidate commit>
python3 -m venv /opt/omnivia-v06-4
/opt/omnivia-v06-4/bin/python -m pip install --upgrade pip
/opt/omnivia-v06-4/bin/python -m pip install -e . -e packages/omnivia-core-runtime

mkdir -p /srv/conformance-workspace
printf 'ws-conformance-synthetic\n' \
  > /srv/conformance-workspace/SYNTHETIC-CONFORMANCE-WORKSPACE

python -c 'import secrets; print(secrets.token_urlsafe(48))' > /srv/bearer
chmod 0600 /srv/bearer

/opt/omnivia-v06-4/bin/python conformance/tls/host.py \
  --address <public IPv4> --port <core tls port> \
  --certificate-chain /etc/letsencrypt/live/<public IPv4>/fullchain.pem \
  --private-key /etc/letsencrypt/live/<public IPv4>/privkey.pem \
  --workspace /srv/conformance-workspace \
  --bearer-file /srv/bearer \
  --evidence /srv/launch-evidence.json
```

The virtual environment is deliberately outside the checkout. That avoids the
host image's system-Python package policy and keeps environment state out of the
tree whose cleanliness the launcher verifies.

It prints the bound URL, the leaf fingerprint and the commit it will attest. The
fingerprint is what `OMNIVIA_TLS_CONFORMANCE_CHAIN_SHA256` must be set to. It
never prints the bearer and never prints a path, and the launch record it writes
carries neither, nor any byte of the private key.

Every refusal happens before a socket exists: a loopback or non-literal address,
a port outside 1–65535, a chain or key that cannot be read as text, a bearer file
that is not mode `0600` or is shorter than 32 characters, a workspace with no
`SYNTHETIC-CONFORMANCE-WORKSPACE` marker, a checkout `git rev-parse` cannot
identify, or a checkout carrying staged, unstaged or untracked changes. There is
no state in which this listener is up and one of those is
true. Each refusal names the **option** and never its value: a path, a credential
or a key byte is either something the operator typed themselves or something this
process must not publish, so an unreadable file produces `--private-key could not
be read as UTF-8 text` rather than an `OSError` quoting where it looked.

The launch record is the one thing that cannot be written first, because it
reports the URL the socket actually bound. So the bind and that write are one
unit: if either fails the listener is retired before the process exits, and there
is no state in which the port is open and no evidence accounts for it.

## Running the suite

Normally: the **Core TLS conformance** workflow, dispatched with the candidate's
full 40-character commit SHA, using the `tls-conformance` environment. Locally,
against an already-provisioned host:

```bash
export OMNIVIA_TLS_CONFORMANCE_HOST=<public IPv4>            # a literal, never a name
export OMNIVIA_TLS_CONFORMANCE_PORT=<core tls port>
export OMNIVIA_TLS_CONFORMANCE_BEARER=...                    # temporary, revocable
export OMNIVIA_TLS_CONFORMANCE_OPERATION=conformance.attest
export OMNIVIA_TLS_CONFORMANCE_PURPOSE=conformance
export OMNIVIA_TLS_CONFORMANCE_CHAIN_SHA256=...              # printed by host.py
export OMNIVIA_TLS_CONFORMANCE_EVIDENCE=/tmp/tls-conformance-evidence.json

python -m pytest conformance/tls -q -rs
```

Every variable is required. A missing one raises `HostConfigurationMissing`
naming it; nothing skips. Run it from a clean checkout: a runner with staged,
unstaged or untracked changes raises `HostConfigurationMissing` too, because the
tree it would compare against the host's is the committed one.

`conformance/` is outside the acceptance gate's Ruff and mypy target lists — it
is outside every collection root by design — so its quality bar is run
explicitly:

```bash
python -m ruff check conformance
python -m mypy --strict conformance/tls/host.py conformance/tls/test_tls_conformance.py
```

| Variable | What it is |
| --- | --- |
| `..._HOST` | the droplet's **public IPv4 literal**; a name is refused, not resolved |
| `..._PORT` | the one open TCP port |
| `..._BEARER` | the temporary credential in the host's `--bearer-file` |
| `..._OPERATION` | `conformance.attest`, asserted rather than trusted |
| `..._PURPOSE` | `conformance` |
| `..._CHAIN_SHA256` | SHA-256 of the DER of the configured leaf, from `host.py` |
| `..._EVIDENCE` | absolute path for the evidence artifact, **outside the work tree** |

## Zero skips, and what no host buys you

Nothing here can pass without a host, deliberately. `host` is session-scoped and
autouse, so the locally generated negative cases cannot be green on a runner that
never reached a listener. An absent or misconfigured host produces **28 errors
and zero skips** — verified, not assumed — and the evidence artifact is still
written and still uploaded.

That is the whole resolution of the zero-skip rule: `conformance/` sits outside
`testpaths`, outside every path `core-acceptance.yml` names, and outside
`phase2-platform.yml`, so nothing invokes this suite except the dispatch-only
workflow. "Not run" and "passed" stay different states without a marker. The
workflow does not read the exit code either: it parses the JUnit record and
requires exactly 28 cases with no `failure`, `error` or `skipped` element on any
of them.

## Where the anchors come from

* **`public_client`** — `ssl.create_default_context()`, public trust store,
  `check_hostname` on, `CERT_REQUIRED`. The only context ever pointed at the
  host. No bundle is derived from the chain under test; a self-signed host
  certificate is a provisioning defect, not a client option.
* **`local_client`** — the same constructor anchored on the generated conformance
  CA. Not laxer; differently anchored, and it never touches the host. It exists
  because an expired chain under an *untrusted* issuer reports `verify_code` 20
  rather than 10, so a case asserting 20 there would keep passing after expiry
  checking broke.

Generated chains are made by the `openssl` CLI into `tmp_path_factory` and
presented through paired `ssl.MemoryBIO`s — no port is bound, nothing is left
behind, and they can never be reached from outside the process. They are issued
for `198.51.100.7` (TEST-NET-2, RFC 5737), so they are unusable against anything
real, and they carry the same `iPAddress`-only SAN shape the host's chain does.

Attribution, all verified: expired → 10, untrusted issuer → 20, self-signed →
18, wrong address → 64, downgrade → a peer protocol-version alert.

## Divergences found while building this — for PM

Recorded rather than resolved at lane level.

1. **A domain-free lane needs a CA that issues for IP identifiers, and those
   certificates are short-lived by policy.** The provisioning and the run have to
   happen inside the same window. This is a scheduling constraint on the closeout,
   not a defect.

2. **ACME `http-01` requires port 80 to be reachable during issuance**, which is
   the one moment the droplet exposes anything but the Core TLS port. The
   sequencing above closes it before the listener starts, and `ufw status
   verbose` after step 3 is the record that it was closed. Nothing in the suite
   can prove a port was shut before it began; that evidence is the operator's.

3. **"Assert on the dispatcher, not the status code" is not reachable from an
   inbound client.** The merged loopback suites use a recording dispatcher
   in-process; a network peer sees only the wire. The substitute here is
   attributable rather than a bare status check: the unauthenticated request
   carries a body that is *independently* unacceptable (wrong media type,
   non-canonical JSON), and the answer must still be `401`. A `401` that beats
   the `415` and the `400` is a refusal taken before the body was read.

4. **Certificate-load failure on the host is not re-provable from here.** The
   hermetic sub-lane proves it in-process. This suite covers the reachable
   surfaces: handshake failures, endpoint-parse refusals, and every response.

5. **The Decision 4 regression runs on the runner, not the host.** An inbound
   client can start no process on the conformance droplet. It is the same
   artifact at the same commit — which, unlike in the previous shape of this
   lane, is now *proven* by `conformance.attest` rather than asserted.

6. **`conformance/` is intentionally outside `testpaths` and is the one
   root-bounded dispatch-only exemption in
   `scripts/check-test-collection.py`.** The checker names exactly
   `DISPATCH_ONLY_TREES = frozenset({"conformance"})`; the acceptance guard pairs
   that exemption with `core-tls-conformance.yml` and requires the workflow to
   name every test-bearing directory under the tree. Ordinary bare runs remain
   complete for every non-conformance repository test.
