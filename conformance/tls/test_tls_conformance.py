"""Hosted non-loopback TLS conformance for the OmniVia Core HTTP adapter (V06-4).

What this suite is, and what a pass means
------------------------------------------
This is the conformance *client*. Core runs on the provisioned conformance host,
started by `conformance/tls/host.py` from this same tree, and this process
connects inbound over TLS to that host's **public IPv4 address**. There is no DNS
name anywhere in this lane: the certificate carries an `iPAddress` SAN and
nothing else, and every name-shaped assertion below is an address assertion. The
private key never reaches here; this side receives only what the listener
presents on the wire.

A pass is **adapter conformance, and nothing wider**. A V06-4 GO confirms the
positive and negative conformance of the hosted TLS HTTP adapter, and no
production console path, credential resolver or serving lifecycle invokes that
adapter. OmniVia Core does not serve HTTP as a production capability, and nothing
in this suite or its evidence may be read as saying it does.
`test_the_standard_service_still_refuses_to_serve_http` is the regression that
keeps that true rather than merely stated. Nor is any of this a claim about
client certificates: Core verifies none, mutual TLS is out of scope, and the
listener asks for none.

Host attestation, and why it is the point
------------------------------------------
The older shape of this lane proved that *a* listener presented a valid chain. It
could not prove which build was behind it, so "the host runs the candidate"
rested on whoever provisioned the droplet. It does not any more:
`conformance.attest` is the one operation the host grants, it is reachable only
with the temporary bearer, and it returns the **committed** commit and tree the
launcher read from its own checkout **before it bound**. This runner computes the
same two values from its own checkout and requires exact equality. That is what
`candidate_identity_match` records, and it is the difference between certifying
this candidate and certifying whatever happened to be running.

`HEAD^{tree}` is the tree of a commit, not a reading of the files on disk, so
neither SHA would notice a checkout someone edited. Cleanliness is therefore a
separately enforced precondition on both sides -- the launcher refuses to bind
from a checkout with staged, unstaged or untracked changes, and :func:`_candidate`
refuses to report from one -- and the equality above is a working-state claim
only because those two refusals happen first.

Why nothing here can skip
--------------------------
This suite records **zero skipped tests**; an unavailable host or a skipped run
retains NO-GO. There is no third outcome, so there is no `skipif`, no
`importorskip` and no `pytest.skip` in this tree, and
`tests/test_core_acceptance_workflow.py` holds that open by parsing the source
rather than trusting the convention. It also pins the count: exactly 28 cases,
and the workflow proves 28 *passed* with no other outcome from the JUnit record.

A suite that fails when the host is down would redden every unrelated pull
request. It does not, and the reason is collection topology rather than a marker.
`conformance/` is outside `[tool.pytest.ini_options] testpaths` and outside every
path `core-acceptance.yml` and `phase2-platform.yml` name; the only thing that
runs it is `.github/workflows/core-tls-conformance.yml`, which is
`workflow_dispatch` only. So the suite is *not invoked* unless someone
deliberately invokes it, and when it is invoked it either passes against a real
host or goes red. "Not run" and "passed" stay different states without a marker.

Missing configuration is therefore a failure, named. :func:`_required` raises
rather than skipping, and because `host` below is session-scoped and autouse,
every test in the file depends on a completed handshake with the real listener --
including the locally generated negative cases, which would otherwise be green
hermetically and read as partial success. An absent host produces 28 errors and
zero skips.

Where the trust anchor comes from
----------------------------------
A test that trusts the certificate it is testing against proves nothing. So:

- :func:`public_client` is built once by `ssl.create_default_context()` --
  `check_hostname` True, `verify_mode` `CERT_REQUIRED`, anchored on the **public
  trust store** and on nothing this run produced. It is the only context ever
  pointed at the host, and `_dial` defaults its `server_hostname` so it cannot
  quietly become otherwise. No CA bundle is derived from the listener, and CI
  holds no private key.
- :func:`local_client` is the same constructor anchored on the locally generated
  conformance CA. It is not laxer -- same address checking, same verify mode --
  it is *differently anchored*, and it never touches the host. It exists because
  three of the refusals are only attributable with a trusted issuer: an expired
  certificate under an untrusted CA reports `verify_code` 20 (no issuer) rather
  than 10 (expired), and a case asserting 20 there would keep passing after
  expiry checking broke.

The defective chains are generated at run time by the `openssl` CLI into pytest's
`tmp_path_factory`, and adding a dependency solely for test certificate
generation is prohibited. They are presented through :func:`_handshake`, which
drives a full TLS handshake between two `ssl.MemoryBIO` pairs. Nothing binds a
port, nothing listens, nothing is left to clean up, and the negative cases cannot
accidentally become reachable from outside this process. They are issued for
`198.51.100.7` -- TEST-NET-2, reserved by RFC 5737 and routable to nothing -- so
they can never be usable against anything real, and they carry the same
`iPAddress`-only SAN shape the host's own chain does.
"""

from __future__ import annotations

import atexit
import hashlib
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest
from omnivia_core_runtime.service.http_transport import (
    APPLICATION_PATH,
    CONTENT_TYPE,
    PROBE_PATH,
    HttpTransportError,
    parse_http_endpoint,
)
from omnivia_core_runtime.service.main import build_parser
from omnivia_core_runtime.service.ovc1 import canonical_json_bytes

# --- configuration ----------------------------------------------------------

HOST_VARIABLE = "OMNIVIA_TLS_CONFORMANCE_HOST"
PORT_VARIABLE = "OMNIVIA_TLS_CONFORMANCE_PORT"
BEARER_VARIABLE = "OMNIVIA_TLS_CONFORMANCE_BEARER"
OPERATION_VARIABLE = "OMNIVIA_TLS_CONFORMANCE_OPERATION"
PURPOSE_VARIABLE = "OMNIVIA_TLS_CONFORMANCE_PURPOSE"
CHAIN_VARIABLE = "OMNIVIA_TLS_CONFORMANCE_CHAIN_SHA256"
EVIDENCE_VARIABLE = "OMNIVIA_TLS_CONFORMANCE_EVIDENCE"

#: The one operation the host grants, and the only way this suite may learn what
#: the host is. Asserted rather than read as configuration: a run that attested
#: through some other operation would be certifying a grant nobody approved.
ATTEST_OPERATION = "conformance.attest"

#: A path this run plants and then looks for in everything that comes back. It is
#: a plausible key location on purpose: `ssl` exceptions quote file paths, and the
#: point is to prove none of them is republished to a caller.
PLANTED_PATH = "/etc/omnivia/planted-conformance-key.pem"

#: TEST-NET-2. Every locally generated chain is issued for this address, and the
#: endpoint-parser cases use it too. Reserved by RFC 5737, routable to nothing.
GENERATED_ADDRESS = "198.51.100.7"

#: TEST-NET-3. The address the presented chain cannot be valid for, used to reach
#: the host under a name it does not hold.
UNMATCHED_ADDRESS = "203.0.113.7"

TIMEOUT_SECONDS = 20.0

#: The evidence record, accumulated as the tests establish each fact and written
#: once at interpreter exit. `atexit` rather than a fixture finalizer: the record
#: has to survive a run that ended badly, and the workflow uploads it whatever
#: the outcome was.
EVIDENCE: dict[str, Any] = {}


@atexit.register
def _write_evidence() -> None:
    destination = os.environ.get(EVIDENCE_VARIABLE, "").strip()
    if not destination:
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(EVIDENCE, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


class HostConfigurationMissing(RuntimeError):
    """Required conformance-host configuration was absent.

    A distinct type so the failure reads as what it is. This tree may not skip
    when the environment is incomplete: an absent host must be loud, and it must
    retain NO-GO rather than produce a green summary line.
    """


def _required(variable: str) -> str:
    value = os.environ.get(variable, "").strip()
    if not value:
        raise HostConfigurationMissing(
            f"{variable} is unset or empty, so this run cannot reach the V06-4 "
            "conformance host. This is a failure and not a skip: the hosted suite "
            "records zero skipped tests, and an unavailable host retains NO-GO "
            "rather than closing V06-4. Provision the host with "
            "conformance/tls/host.py and supply the workflow environment."
        )
    return value


# --- reaching the host ------------------------------------------------------


@pytest.fixture(scope="session")
def address() -> str:
    """The host's public IPv4 literal. A name would be refused, not resolved."""
    raw = _required(HOST_VARIABLE)
    try:
        parsed = ipaddress.ip_address(raw)
    except ValueError:
        parsed = None
    if not isinstance(parsed, ipaddress.IPv4Address):
        raise HostConfigurationMissing(
            f"{HOST_VARIABLE} must be the host's public IPv4 literal. This lane is "
            "domain-free: the certificate carries an iPAddress SAN, no DNS name is "
            "issued for, and a name here would be verified against a SAN that does "
            "not exist."
        )
    # Stated positively: the address must be one a certificate authority will
    # issue for and a runner can route to. "Not private and not loopback" is a
    # weaker claim that admits the whole reserved 240.0.0.0/4, the carrier-grade
    # 100.64.0.0/10, the unspecified address and every multicast group -- none of
    # which is a host, so a run against one would fail somewhere further in for a
    # reason that says nothing about Core. `is_global` covers all of those except
    # multicast, which it reports as global because 224.0.0.0/4 is neither
    # private nor assignable to a listener.
    if not parsed.is_global or parsed.is_multicast:
        raise HostConfigurationMissing(
            f"{HOST_VARIABLE} is not a usable public IPv4 address, so a run "
            "against it proves nothing about a non-loopback listener reachable "
            "from a runner"
        )
    return str(parsed)


@pytest.fixture(scope="session")
def port() -> int:
    """The one open TCP port, inside the range a socket can actually carry.

    Range-checked rather than merely digit-checked: `0` asks the OS to choose,
    which is meaningless to a client, and anything above 65535 is not a port at
    all. Both would otherwise reach `socket.create_connection` and fail there --
    an `OverflowError` or a connection to whatever is on the ephemeral port the
    kernel picked, neither of which reads as the configuration error it is.
    """
    raw = _required(PORT_VARIABLE)
    if not raw.isascii() or not raw.isdigit():
        raise HostConfigurationMissing(f"{PORT_VARIABLE} is not a port number")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise HostConfigurationMissing(
            f"{PORT_VARIABLE} is outside the 1-65535 range a TCP port occupies"
        )
    return port


@pytest.fixture(scope="session")
def bearer() -> str:
    return _required(BEARER_VARIABLE)


@pytest.fixture(scope="session")
def public_client() -> ssl.SSLContext:
    """The one strict client, anchored on the public trust store.

    Built once and reused for the positive case and every host-facing negative
    case. Nothing loads an additional anchor into it, nothing relaxes it for a
    negative case, and the assertions below are here so a later edit that did
    either fails immediately rather than at review. In particular no bundle is
    ever derived from the chain under test: the certificate has to chain to an
    authority the public trust store already holds, which is the whole of what
    makes an independent client's verdict worth anything.
    """
    context = ssl.create_default_context()
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    return context


def _dial(
    context: ssl.SSLContext,
    address: str,
    port: int,
    server_hostname: str | None = None,
) -> ssl.SSLSocket:
    """One TLS connection to the conformance host.

    `server_hostname` defaults to the address being connected to, and the only
    caller that passes anything else is the wrong-address case, which has to.
    Python hands an IP literal to `X509_VERIFY_PARAM_set1_ip_asc` rather than
    `set1_host`, so what is checked is the `iPAddress` SAN.
    """
    plain = socket.create_connection((address, port), timeout=TIMEOUT_SECONDS)
    try:
        return context.wrap_socket(plain, server_hostname=server_hostname or address)
    except BaseException:
        plain.close()
        raise


@pytest.fixture(scope="session", autouse=True)
def host(public_client: ssl.SSLContext, address: str, port: int) -> dict[str, Any]:
    """Complete one verified handshake, and record what the listener presented.

    Autouse and session-scoped **deliberately**. Several of the negative cases are
    locally generated and never touch the host, so without this every one of them
    would pass on a runner with no host in sight and the summary line would read
    like partial conformance. Depending on this fixture makes the host a
    precondition of the whole file: if it is absent, every test errors at setup,
    which is neither a pass nor a skip.
    """
    with _dial(public_client, address, port) as connection:
        presented = connection.getpeercert()
        der = connection.getpeercert(binary_form=True)
        negotiated = {
            "tls_version": connection.version(),
            "cipher": connection.cipher(),
        }
    assert presented is not None
    assert isinstance(der, bytes)

    facts = {
        "address": address,
        "port": port,
        "negotiated": negotiated,
        "leaf_sha256": hashlib.sha256(der).hexdigest(),
        "certificate": _described(presented),
    }
    EVIDENCE["host"] = facts
    EVIDENCE["client"] = {
        "python": sys.version,
        "openssl": ssl.OPENSSL_VERSION,
        "anchor": "system trust store (ssl.create_default_context)",
    }
    return facts


def _described(certificate: Mapping[str, Any]) -> dict[str, Any]:
    """Subject, issuer, serial, validity window and both SAN kinds."""

    def flatten(field: Any) -> dict[str, str]:
        return {name: value for group in field or () for name, value in group}

    alternative = certificate.get("subjectAltName", ())
    return {
        "subject": flatten(certificate.get("subject")),
        "issuer": flatten(certificate.get("issuer")),
        "serial_number": certificate.get("serialNumber"),
        "not_before": certificate.get("notBefore"),
        "not_after": certificate.get("notAfter"),
        # `ssl` spells the two SAN kinds "IP Address" and "DNS". Both are recorded
        # because the claim is about the *absence* of one as much as the presence
        # of the other.
        "ip_address_san": [
            value for kind, value in alternative if kind == "IP Address"
        ],
        "dns_san": [value for kind, value in alternative if kind == "DNS"],
    }


# --- the wire ---------------------------------------------------------------


def _exchange(
    connection: ssl.SSLSocket, head: Sequence[str], body: bytes
) -> tuple[int, bytes, bytes]:
    """One request/response over an established TLS connection.

    The head is returned as well as the body. Every refusal this adapter makes
    carries an empty body by construction, so a response *header* is the one place
    in a refusal where a planted value could still survive, and the redaction
    proofs assert on both halves rather than on the half that is empty anyway.

    No `shutdown(SHUT_WR)` after sending: on an `ssl.SSLSocket` that is not a
    half-close -- it tears the session down in both directions -- and the adapter
    answers `Connection: close` regardless, so reading to EOF terminates without
    one.
    """
    raw = ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body
    connection.sendall(raw)
    chunks: list[bytes] = []
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    response = b"".join(chunks)
    head_bytes, _, response_body = response.partition(b"\r\n\r\n")
    status = int(head_bytes.split(b"\r\n", 1)[0].split(b" ")[1])
    return status, head_bytes, response_body


def _post(
    context: ssl.SSLContext,
    address: str,
    port: int,
    path: str,
    body: bytes,
    *,
    credential: str | None = None,
    content_type: str = CONTENT_TYPE,
    extra: Sequence[str] = (),
) -> tuple[int, bytes, bytes]:
    head = [
        f"POST {path} HTTP/1.1",
        f"Host: {address}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
    ]
    if credential is not None:
        head.insert(2, f"Authorization: Bearer {credential}")
    head.extend(extra)
    with _dial(context, address, port) as connection:
        return _exchange(connection, head, body)


def _probe(kind: str) -> bytes:
    return canonical_json_bytes({"probe": kind})


def _request(
    operation: str,
    purpose: str,
    api_version: str,
    *,
    workspace: str | None = None,
    scopes: Sequence[str] = (),
    principal: str | None = None,
) -> bytes:
    """A `RequestEnvelope` as canonical JSON bytes.

    `api_version` is the version **the server reported on a probe**, not a
    constant this client asserts: using what the host says to build the request is
    what makes the record a fact about the host rather than about this checkout.
    """
    metadata: dict[str, Any] = {
        "request_id": "req-conformance-1",
        "correlation_id": "corr-conformance-1",
        "trace_id": "trace-conformance-1",
        "api_version": api_version,
        "client": {"id": "omnivia-tls-conformance", "version": "0.1.0"},
        "scopes": list(scopes),
        "purpose": purpose,
        "required_capabilities": [],
    }
    if workspace is not None:
        metadata["workspace_id"] = workspace
    if principal is not None:
        metadata["principal_claim"] = {"claimed_principal_id": principal}
    return canonical_json_bytes(
        {"operation": operation, "metadata": metadata, "input": {}}
    )


@pytest.fixture(scope="session")
def reported(public_client: ssl.SSLContext, address: str, port: int) -> dict[str, Any]:
    """What the server says it is, read off an unauthenticated health probe."""
    status, _, body = _post(
        public_client, address, port, PROBE_PATH, _probe("service.health")
    )
    assert status == 200, f"health probe answered {status}"
    document = json.loads(body)
    EVIDENCE["server_reported"] = {
        "api_version": document["api_version"],
        "server_version": document["server_version"],
        "probe_status": document["status"],
    }
    return dict(document)


# --- generated certificate material ----------------------------------------


def _openssl(*args: str) -> str:
    """Run the `openssl` CLI, or fail loudly.

    If the generator is unavailable the test **fails**, it does not skip. So a
    missing binary raises here rather than reaching a marker.
    """
    try:
        completed = subprocess.run(
            ["openssl", *args], capture_output=True, text=True, check=False
        )
    except OSError as error:
        raise HostConfigurationMissing(
            "the `openssl` CLI is unavailable, and it is what generates the test "
            "certificates. This is a failure and not a skip. Underlying error "
            f"class: {type(error).__name__}"
        ) from None
    if completed.returncode != 0:
        raise HostConfigurationMissing(
            f"`openssl {args[0]}` exited {completed.returncode} while generating "
            "conformance certificate material"
        )
    return completed.stdout


class Material:
    """Locally generated chains, all of them inside a test-owned temporary tree.

    Nothing here is ever written into the work tree: generated keys, certificates
    and bundles land only beneath `tmp_path_factory`. Every chain carries an
    `iPAddress` SAN for TEST-NET-2 and no DNS name, which is the same shape the
    host's own certificate has -- so the calibration below exercises the address
    matching path this lane actually depends on.
    """

    address = GENERATED_ADDRESS

    #: Fixed rather than derived from `hash()`, which is salted per process and
    #: would make two runs of the same suite produce different evidence.
    serials: ClassVar[dict[str, int]] = {"valid": 11, "untrusted": 12}

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.authority_key, self.authority = self._authority("conformance")
        self.other_key, self.other = self._authority("unrelated")
        self._extensions = directory / "leaf.ext"
        self._extensions.write_text(
            f"subjectAltName=IP:{self.address}\n", encoding="utf-8"
        )
        self.valid_key, self.valid = self._signed(
            "valid", self.authority_key, self.authority
        )
        self.untrusted_key, self.untrusted = self._signed(
            "untrusted", self.other_key, self.other
        )
        self.expired_key, self.expired = self._expired()
        self.self_signed_key, self.self_signed = self._self_signed()

    def _authority(self, label: str) -> tuple[Path, Path]:
        key = self.directory / f"{label}-ca.key"
        certificate = self.directory / f"{label}-ca.crt"
        _openssl(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(certificate),
            "-days", "1", "-subj", f"/CN={label}-conformance-ca",
            "-addext", "basicConstraints=critical,CA:TRUE",
        )
        return key, certificate

    def _request_for(self, label: str) -> tuple[Path, Path]:
        key = self.directory / f"{label}.key"
        request = self.directory / f"{label}.csr"
        _openssl(
            "req", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(request), "-subj", "/CN=conformance-leaf",
        )
        return key, request

    def _signed(
        self, label: str, authority_key: Path, authority: Path
    ) -> tuple[Path, Path]:
        key, request = self._request_for(label)
        certificate = self.directory / f"{label}.crt"
        _openssl(
            "x509", "-req", "-in", str(request),
            "-CA", str(authority), "-CAkey", str(authority_key),
            "-out", str(certificate), "-extfile", str(self._extensions),
            "-set_serial", str(self.serials[label]), "-days", "1",
        )
        return key, certificate

    def _expired(self) -> tuple[Path, Path]:
        """A chain the trusted CA issued, whose validity window has passed.

        `openssl x509 -req` cannot date a certificate into the past -- `-days`
        with a negative value is rejected outright, and the
        `-not_before`/`-not_after` flags that would do it are too new to rely on.
        `openssl ca -startdate/-enddate` has been stable for far longer and is
        what is used here, at the cost of the small database the subcommand
        insists on.

        It matters that the *trusted* CA signs this one. Under an untrusted issuer
        the same certificate reports `verify_code` 20 (no local issuer) rather
        than 10 (expired), and a case asserting 20 there would still pass after
        expiry checking stopped working.
        """
        key, request = self._request_for("expired")
        certificate = self.directory / "expired.crt"
        database = self.directory / "index.txt"
        database.write_text("", encoding="utf-8")
        (self.directory / "serial").write_text("1000\n", encoding="utf-8")
        configuration = self.directory / "ca.cnf"
        configuration.write_text(
            "[ca]\ndefault_ca=conformance\n\n"
            "[conformance]\n"
            f"new_certs_dir={self.directory}\n"
            f"database={database}\n"
            f"serial={self.directory / 'serial'}\n"
            "default_md=sha256\npolicy=anything\nemail_in_dn=no\n"
            "rand_serial=no\ncopy_extensions=none\nx509_extensions=leaf\n"
            "unique_subject=no\n\n"
            "[anything]\ncommonName=optional\ncountryName=optional\n"
            "stateOrProvinceName=optional\norganizationName=optional\n"
            "organizationalUnitName=optional\n\n"
            f"[leaf]\nsubjectAltName=IP:{self.address}\n",
            encoding="utf-8",
        )
        _openssl(
            "ca", "-batch", "-config", str(configuration),
            "-cert", str(self.authority), "-keyfile", str(self.authority_key),
            "-in", str(request), "-out", str(certificate),
            "-startdate", "20200101000000Z", "-enddate", "20200102000000Z",
        )
        return key, certificate

    def _self_signed(self) -> tuple[Path, Path]:
        key = self.directory / "self-signed.key"
        certificate = self.directory / "self-signed.crt"
        _openssl(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(certificate), "-days", "1",
            "-subj", "/CN=conformance-leaf",
            "-addext", f"subjectAltName=IP:{self.address}",
        )
        return key, certificate

    def described(self, certificate: Path) -> dict[str, str]:
        """Subject, issuer, serial and validity for the evidence record."""
        text = _openssl(
            "x509", "-in", str(certificate), "-noout",
            "-subject", "-issuer", "-serial", "-dates",
        )
        found: dict[str, str] = {}
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                found[key.strip()] = value.strip()
        return found


@pytest.fixture(scope="session")
def material(tmp_path_factory: pytest.TempPathFactory) -> Material:
    made = Material(tmp_path_factory.mktemp("tls-conformance-material"))
    EVIDENCE["generated_chains"] = {
        label: made.described(getattr(made, label))
        for label in ("valid", "expired", "untrusted", "self_signed")
    }
    return made


@pytest.fixture(scope="session")
def local_client(material: Material) -> ssl.SSLContext:
    """The same strict client, anchored on the generated conformance CA only.

    Not a laxer context -- `check_hostname` stays True and `verify_mode` stays
    `CERT_REQUIRED`, asserted below -- and never pointed at the host. It exists so
    the expired case can be attributed to expiry rather than to a missing issuer;
    see :meth:`Material._expired`.
    """
    context = ssl.create_default_context(cafile=str(material.authority))
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    return context


def _handshake(
    client: ssl.SSLContext, certificate: Path, key: Path, server_hostname: str
) -> str:
    """Drive a full handshake between two memory BIOs and report the outcome.

    No socket, no thread, no port: the defective chains are never reachable from
    outside this process, and there is nothing to leave behind if a case fails.
    The server side mirrors the shipped adapter's own floor (TLS 1.2, and no
    client certificate requested) so what is under test is the chain rather than
    an incidental difference in how the peer was configured.
    """
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.minimum_version = ssl.TLSVersion.TLSv1_2
    server.load_cert_chain(certfile=certificate, keyfile=key)

    # Two BIOs, shared rather than copied: what one side writes out *is* what the
    # other reads in, so there is no pump step and nothing to get backwards. The
    # four-BIO arrangement with a copy between them is the obvious way to write
    # this and it is easy to wire a side's own output back into its own input,
    # which handshakes into `UNEXPECTED_MESSAGE` -- a plausible-looking refusal
    # that is not the certificate verification failure under test.
    client_to_server, server_to_client = ssl.MemoryBIO(), ssl.MemoryBIO()
    us = client.wrap_bio(
        server_to_client, client_to_server, server_hostname=server_hostname
    )
    peer = server.wrap_bio(client_to_server, server_to_client, server_side=True)

    for _ in range(20):
        for side in (us, peer):
            try:
                side.do_handshake()
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLCertVerificationError as error:
                return f"verify:{error.verify_code}:{error.verify_message}"
            except ssl.SSLError as error:
                return f"alert:{error.reason}"
        if us.cipher() is not None and peer.cipher() is not None:
            return "established"
    return "unsettled"


# --- the positive case ------------------------------------------------------


def test_a_valid_chain_with_the_exact_ip_address_san_succeeds(
    host: dict[str, Any], address: str, reported: Mapping[str, Any]
) -> None:
    """The calibration every negative case rests on, and the domain-free claim.

    The `host` fixture completed the handshake with the strict public-trust-store
    client; this asserts what it negotiated and what it was handed. Without a
    success here, the refusals below would prove only that the client refuses
    everything.

    The SAN assertion is exact in both directions: the configured public IPv4 is
    the *whole* of the `iPAddress` SAN set, and there is no DNS SAN at all. A
    certificate carrying an extra address would be valid for a host this lane
    never provisioned, and one carrying a name would mean the domain-free
    provisioning route was not the one taken.
    """
    assert host["negotiated"]["tls_version"] in {"TLSv1.2", "TLSv1.3"}
    assert host["negotiated"]["cipher"] is not None
    assert host["certificate"]["not_after"]
    assert host["certificate"]["ip_address_san"] == [address], (
        "the presented chain's iPAddress SAN is not exactly the configured public "
        f"IPv4: {host['certificate']['ip_address_san']}"
    )
    assert host["certificate"]["dns_san"] == [], (
        "the presented chain carries a DNS SAN; this lane is domain-free and no "
        "name is issued for or verified against"
    )
    assert reported["status"] in {"pass", "warn"}


def test_nothing_terminated_tls_between_this_client_and_core(
    host: dict[str, Any]
) -> None:
    """The anti-proxy proof.

    A TLS-terminating proxy in between would make every claim above a property of
    the proxy, and the evidence would certify the wrong software. The listener's
    configured chain is fingerprinted on the host by `host.py` before it binds and
    supplied as `OMNIVIA_TLS_CONFORMANCE_CHAIN_SHA256`; the leaf actually
    presented to this socket must be the same bytes.
    """
    expected = _required(CHAIN_VARIABLE).lower().replace(":", "")
    assert host["leaf_sha256"] == expected, (
        "the certificate presented to this client is not the chain the host was "
        "configured with, so something terminated TLS in between"
    )


# --- the refusals -----------------------------------------------------------


def test_an_incorrect_address_fails(
    public_client: ssl.SSLContext, address: str, port: int, host: dict[str, Any]
) -> None:
    """The wrong-peer row, against the real chain.

    Run against the host rather than a generated chain on purpose: this is a claim
    about what Core presents. The chain verifies, and then the address does not
    match, which is a different and stronger failure than "an untrusted
    certificate was refused". OpenSSL reports 64, `IP_ADDRESS_MISMATCH` -- not 62,
    which is the *hostname* mismatch a DNS-named lane would see, and asserting 62
    here would pass only if the address checking under test never ran.
    """
    with pytest.raises(ssl.SSLCertVerificationError) as raised:
        _dial(public_client, address, port, server_hostname=UNMATCHED_ADDRESS)

    error = raised.value
    assert error.verify_code == 64, f"expected an IP address mismatch, got {error!r}"
    assert "ip address mismatch" in (error.verify_message or "").lower()
    assert UNMATCHED_ADDRESS in str(error)
    assert UNMATCHED_ADDRESS not in host["certificate"]["ip_address_san"]


def test_the_strict_client_accepts_a_well_formed_local_chain(
    local_client: ssl.SSLContext, material: Material, host: dict[str, Any]
) -> None:
    """The calibration for the three generated refusals below.

    Same context, same generation route, same `iPAddress` SAN shape, no defect: it
    must succeed. Three failures and no success would prove only that the harness
    is broken.
    """
    outcome = _handshake(
        local_client, material.valid, material.valid_key, material.address
    )
    assert outcome == "established", outcome


@pytest.mark.parametrize(
    ("defect", "verify_code", "phrase"),
    [
        ("expired", 10, "expired"),
        ("untrusted", 20, "unable to get local issuer certificate"),
        ("self_signed", 18, "self-signed"),
    ],
)
def test_a_defective_chain_is_refused_for_its_own_reason(
    local_client: ssl.SSLContext,
    material: Material,
    host: dict[str, Any],
    defect: str,
    verify_code: int,
    phrase: str,
) -> None:
    """Attributable, never `pytest.raises(Exception)`.

    The specific `verify_code` is asserted because a test that only checks
    "something raised" passes for the wrong reason forever, and keeps passing
    after the thing it was meant to catch is broken. Codes are OpenSSL's:
    10 `CERT_HAS_EXPIRED`, 20 `UNABLE_TO_GET_ISSUER_CERT_LOCALLY`,
    18 `DEPTH_ZERO_SELF_SIGNED_CERT`.
    """
    outcome = _handshake(
        local_client,
        getattr(material, defect),
        getattr(material, f"{defect}_key"),
        material.address,
    )
    kind, _, detail = outcome.partition(":")
    assert kind == "verify", f"expected certificate verification to fail, got {outcome}"
    code, _, message = detail.partition(":")
    assert int(code) == verify_code, f"expected verify_code {verify_code}, got {outcome}"
    assert phrase in message.lower()


def test_a_client_capped_below_the_protocol_floor_is_refused(
    address: str, port: int, host: dict[str, Any]
) -> None:
    """The downgrade row: the floor is TLS 1.2 and it is enforced.

    This is the one case that must build a second context, because capping the
    offered maximum is the whole of what it tests -- the cap cannot be expressed
    on the shared client without destroying it for every other case. It is capped,
    not loosened, in everything that matters: the same trust store, the same
    `check_hostname`, the same `verify_mode`.

    `SECLEVEL=0` is needed because OpenSSL 3 will not *offer* TLS 1.1 at its
    default security level, and a client that cannot offer the protocol would fail
    locally and pass this test for the wrong reason. So the assertion is on the
    peer's alert, and a local "no protocols available" is called out as the
    harness's own failure rather than counted as the host refusing.
    """
    capped = ssl.create_default_context()
    capped.minimum_version = ssl.TLSVersion.TLSv1
    capped.maximum_version = ssl.TLSVersion.TLSv1_1
    capped.set_ciphers("DEFAULT@SECLEVEL=0")
    assert capped.check_hostname is True
    assert capped.verify_mode == ssl.CERT_REQUIRED
    assert capped.maximum_version == ssl.TLSVersion.TLSv1_1

    with pytest.raises(ssl.SSLError) as raised:
        _dial(capped, address, port)

    reason = raised.value.reason or ""
    EVIDENCE["downgrade"] = {
        "offered_maximum": str(capped.maximum_version),
        "reason": reason,
    }
    assert reason != "NO_PROTOCOLS_AVAILABLE", (
        "this runner's OpenSSL would not offer TLS 1.1, so the handshake failed "
        "locally and proves nothing about the host's floor"
    )
    assert "PROTOCOL" in reason or "VERSION" in reason, (
        f"expected a protocol-version alert from the peer, got {reason!r}"
    )
    assert host["negotiated"]["tls_version"] in {"TLSv1.2", "TLSv1.3"}


# --- no cleartext, no fallback ----------------------------------------------


def test_a_cleartext_request_to_the_tls_port_is_not_answered_as_http(
    address: str, port: int, host: dict[str, Any]
) -> None:
    """Plain HTTP is not silently accepted or used as a fallback."""
    body = _probe("service.health")
    request = (
        f"POST {PROBE_PATH} HTTP/1.1\r\nHost: {address}\r\n"
        f"Content-Type: {CONTENT_TYPE}\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode("latin-1") + body
    with socket.create_connection((address, port), timeout=TIMEOUT_SECONDS) as plain:
        plain.sendall(request)
        try:
            answer = plain.recv(65536)
        except OSError:
            answer = b""
    assert not answer.startswith(b"HTTP/"), (
        "the TLS port answered a cleartext request with an HTTP status line"
    )


def test_a_non_loopback_cleartext_endpoint_still_refuses_to_parse(
    host: dict[str, Any]
) -> None:
    """The third part of the downgrade row: no scheme fallback anywhere.

    Asserted against the shipped adapter as installed on this runner. It is the
    same artifact the host runs -- the identity test below is what proves that --
    and endpoint parsing is a property of that artifact rather than of the machine
    it is on. TEST-NET-2 rather than the conformance host's own address, so the
    refusal cannot be about reachability.
    """
    with pytest.raises(HttpTransportError):
        parse_http_endpoint(f"http://{GENERATED_ADDRESS}:8443")
    with pytest.raises(HttpTransportError):
        parse_http_endpoint(f"https://{GENERATED_ADDRESS}:8443")


# --- authentication and the claims gate, over the real transport ------------


def test_an_unauthenticated_request_is_refused_before_the_body_is_read(
    public_client: ssl.SSLContext, address: str, port: int, host: dict[str, Any]
) -> None:
    """"Never reaches dispatch", made attributable from a network peer.

    A recording dispatcher proves it in-process; an inbound conformance client
    cannot see the dispatcher, so asserting a bare `401` here would be exactly the
    status-code check that proves nothing. What *is* observable is the **order**
    of the adapter's checks: the credential is verified before the body length,
    the media type and the document are looked at. So this sends a request that is
    independently unacceptable in two further ways -- an unsupported media type
    and a body that is not canonical JSON -- and requires the answer to be `401`
    rather than the `415` or `400` those would earn. A `401` that wins over both
    is a refusal taken before the body was read.
    """
    status, head, body = _post(
        public_client,
        address,
        port,
        APPLICATION_PATH,
        b"{ this is not canonical json }",
        content_type="text/plain",
    )
    assert status == 401, (
        f"expected 401 to precede the media-type and body checks, got {status}"
    )
    assert body == b"", "a refusal must carry no body"
    assert b"Content-Length: 0" in head


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer ",
        "Bearer not-a-credential-this-host-issued",
        "Basic Y29uZm9ybWFuY2U6Y29uZm9ybWFuY2U=",
        "bearer wrong-scheme-casing-and-value",
    ],
)
def test_a_malformed_or_unknown_bearer_is_refused(
    public_client: ssl.SSLContext,
    address: str,
    port: int,
    host: dict[str, Any],
    reported: Mapping[str, Any],
    authorization: str,
) -> None:
    """A malformed, wrong-scheme or wrong-audience bearer is rejected."""
    body = _request(
        ATTEST_OPERATION, _required(PURPOSE_VARIABLE), reported["api_version"]
    )
    status, _, response = _post(
        public_client,
        address,
        port,
        APPLICATION_PATH,
        body,
        extra=[f"Authorization: {authorization}"],
    )
    assert status == 401
    assert response == b""


def test_a_narrowing_claim_is_accepted(
    public_client: ssl.SSLContext,
    address: str,
    port: int,
    bearer: str,
    host: dict[str, Any],
    reported: Mapping[str, Any],
) -> None:
    """The accepted half of the claims gate.

    Every claim named here is one the resolved session already holds: the purpose
    the host was configured with, and the empty scope set, which is a subset of
    anything. `workspace_id` and `principal_claim` are omitted, which is the "run
    as the session" form. Without this half the four refusals below would prove
    only that the endpoint refuses everything.
    """
    body = _request(
        _required(OPERATION_VARIABLE),
        _required(PURPOSE_VARIABLE),
        reported["api_version"],
    )
    status, _, response = _post(
        public_client, address, port, APPLICATION_PATH, body, credential=bearer
    )
    assert status == 200, f"a narrowing claim was refused with {status}"
    document = json.loads(response)
    assert "metadata" in document
    EVIDENCE["application"] = {
        "status": status,
        "version": document["metadata"].get("version"),
        "authority": document["metadata"].get("authority"),
    }


@pytest.mark.parametrize("widened", ["purpose", "workspace", "scope", "principal"])
def test_a_widening_claim_is_refused(
    public_client: ssl.SSLContext,
    address: str,
    port: int,
    bearer: str,
    host: dict[str, Any],
    reported: Mapping[str, Any],
    widened: str,
) -> None:
    """The refused half: claims narrow and can never widen.

    Each variant names exactly one thing the resolved session does not hold. The
    values are sentinels no provisioning would produce, so a host that happened to
    grant something similar cannot make this pass by accident -- and each is
    pattern-valid for its field, so the refusal is the claims gate's `403` rather
    than a `400` from the decoder that would never have reached it.
    """
    operation = _required(OPERATION_VARIABLE)
    purpose = _required(PURPOSE_VARIABLE)
    api_version = reported["api_version"]
    bodies = {
        "purpose": _request(
            operation, "purpose_this_session_does_not_hold", api_version
        ),
        "workspace": _request(
            operation, purpose, api_version, workspace="ws-not-granted-to-this-session"
        ),
        "scope": _request(operation, purpose, api_version, scopes=["scope:not_granted"]),
        "principal": _request(
            operation, purpose, api_version, principal="not-this-sessions-principal"
        ),
    }
    status, _, response = _post(
        public_client,
        address,
        port,
        APPLICATION_PATH,
        bodies[widened],
        credential=bearer,
    )
    assert status == 403, f"a widening {widened} claim was answered {status}"
    assert response == b""


# --- the Decision 4 regression ----------------------------------------------


def _service(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Invoke the console-script entry point exactly as the installed script does."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys;"
                "from omnivia_core_runtime.service.main import main;"
                "sys.exit(main(sys.argv[1:]))"
            ),
            "--workspace",
            "/nonexistent/conformance-workspace",
            "--installation-state",
            "/nonexistent/conformance-state",
            "--endpoint",
            "unix:///nonexistent/conformance.sock",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_standard_service_still_refuses_to_serve_http(host: dict[str, Any]) -> None:
    """The boundary a V06-4 GO must not be read past.

    A GO is **adapter conformance, not production HTTP serving**. The console
    script supplies no credential resolver, `--http-endpoint` still defaults to
    `None`, and every bind through it still exits 2. `conformance/tls/host.py` is
    an approved embedder that goes nowhere near this entry point, so nothing about
    the hosted run relaxes what is asserted here.

    Attributable rather than "exited 2": three different things make this entry
    point exit 2, and one of them is argparse rejecting the command line, which
    would pass this test while proving nothing. Both refusals below are asserted
    on the exact message the shipped code writes, and both are reached *after*
    argument parsing -- so a run that never got past the parser fails here.

    Run on the runner rather than on the host, which is a deviation the inbound
    topology forces: an inbound conformance client can start no process on the
    conformance host. The distribution under test is the same artifact at the same
    commit -- proven, not assumed, by the identity test below -- and the refusal is
    a property of the artifact rather than of the machine it sits on.
    """
    assert build_parser().get_default("http_endpoint") is None, (
        "--http-endpoint no longer defaults to None"
    )

    # A TLS endpoint cannot even be expressed through this entry point: the scheme
    # promises material the console script has no way to supply.
    refused = _service("--http-endpoint", f"https://{GENERATED_ADDRESS}:8443")
    assert refused.returncode == 2
    assert "HTTP endpoint is not an accepted loopback endpoint" in refused.stderr
    assert "usage:" not in refused.stderr, "argparse rejected the command line"

    # And a loopback endpoint that does parse still refuses, on the resolver.
    resolverless = _service("--http-endpoint", "http://127.0.0.1:8443")
    assert resolverless.returncode == 2
    assert "HTTP needs a trusted credential resolver" in resolverless.stderr
    assert "usage:" not in resolverless.stderr, "argparse rejected the command line"


# --- redaction over the real transport --------------------------------------


@pytest.mark.parametrize(
    ("path", "credential", "body", "content_type"),
    [
        (APPLICATION_PATH, None, b"{}", CONTENT_TYPE),
        (APPLICATION_PATH, "planted", b"{}", "text/plain"),
        (PROBE_PATH, None, b"not json at all", CONTENT_TYPE),
        ("/v1/" + PLANTED_PATH.strip("/"), None, b"{}", CONTENT_TYPE),
    ],
)
def test_no_planted_credential_or_path_survives_into_a_response(
    public_client: ssl.SSLContext,
    address: str,
    port: int,
    bearer: str,
    host: dict[str, Any],
    path: str,
    credential: str | None,
    body: bytes,
    content_type: str,
) -> None:
    """Redaction, over the real transport.

    Both halves of the response are searched, not just the body: every refusal
    this adapter makes carries an empty body by construction, so the body alone
    would be a proof about nothing.
    """
    status, head, response = _post(
        public_client,
        address,
        port,
        path,
        body,
        credential=bearer if credential else None,
        content_type=content_type,
        extra=[f"X-Omnivia-Conformance-Planted: {PLANTED_PATH}"],
    )
    assert status >= 400
    for half in (head, response):
        assert PLANTED_PATH.encode() not in half
        assert bearer.encode() not in half
        assert b"Traceback" not in half


def test_a_failed_handshake_republishes_no_planted_value(
    public_client: ssl.SSLContext,
    address: str,
    port: int,
    host: dict[str, Any],
    bearer: str,
) -> None:
    """The leak surface TLS introduces: `ssl` exceptions quote paths.

    Asserted across the whole exception chain, not just `str(error)` --
    `from None` clears `__cause__` and leaves `__context__` referencing the
    original, one attribute access from anything that logs the refusal.
    """
    with pytest.raises(ssl.SSLCertVerificationError) as raised:
        _dial(public_client, address, port, server_hostname=UNMATCHED_ADDRESS)
    error: BaseException | None = raised.value
    seen: list[str] = []
    while error is not None:
        seen.extend([str(error), repr(error.args)])
        error = error.__cause__ or error.__context__
    joined = "\n".join(seen)
    assert bearer not in joined
    assert PLANTED_PATH not in joined


def test_the_endpoint_parser_republishes_no_caller_supplied_text(
    host: dict[str, Any]
) -> None:
    """The same rule on the refusal a caller can most easily reach."""
    with pytest.raises(HttpTransportError) as raised:
        parse_http_endpoint(f"https://user:{PLANTED_PATH}@{GENERATED_ADDRESS}:8443")
    error: BaseException | None = raised.value
    seen: list[str] = []
    while error is not None:
        seen.extend([str(error), repr(error.args)])
        error = error.__cause__ or error.__context__
    assert PLANTED_PATH not in "\n".join(seen)


# --- host attestation -------------------------------------------------------


def _git(*arguments: str) -> str | None:
    """`git` in this checkout, or None if it could not run or refused."""
    completed = subprocess.run(
        ["git", *arguments],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def _candidate() -> dict[str, str]:
    """The **committed** commit and tree this runner holds, from a clean checkout.

    `HEAD^{tree}` is the tree of the last commit and not the files on disk, so
    equality with the host's `tree_sha` is only a working-state claim while both
    checkouts are known clean. The host enforces that on its side before it binds
    (`_identity` in `conformance/tls/host.py`); this is the same precondition on
    this side, checked before the two are compared rather than inferred from
    them.

    A dirty runner raises rather than returning a value the caller would compare:
    the failure is that this run cannot certify anything, not that the host
    disagrees, and the two must not read alike. Nothing derived from `git status`
    reaches the message -- it names paths, and the evidence artifact is uploaded.
    """
    status = _git("status", "--porcelain", "--untracked-files=all")
    if status is None or status.strip():
        raise HostConfigurationMissing(
            "this runner's checkout is not a readable, clean git checkout: staged, "
            "unstaged or untracked changes -- or a working state git could not "
            "report at all -- mean the committed tree it would compare is not the "
            "tree it holds. The candidate must be certified from a clean checkout; "
            "this is a failure and not a skip."
        )
    found: dict[str, str] = {}
    for label, revision in (("commit_sha", "HEAD"), ("tree_sha", "HEAD^{tree}")):
        found[label] = (_git("rev-parse", revision) or "").strip() or "unavailable"
    return found


def test_the_host_attests_the_commit_and_tree_this_runner_holds(
    public_client: ssl.SSLContext,
    address: str,
    port: int,
    bearer: str,
    host: dict[str, Any],
    reported: Mapping[str, Any],
) -> None:
    """The identity proof, and the only place the host's build is learned.

    The commit and tree come from `conformance.attest` and from nowhere else: not
    from a banner, not from a probe, not from a file the provisioning step wrote.
    That matters twice over. It is authenticated, so an unauthenticated peer
    cannot fingerprint the build behind this listener. And it is the host's own
    reading of its own checkout, taken before it bound, so it cannot be a value
    this runner supplied and then read back.

    Equality is exact and covers both objects, and both are *committed* objects:
    `tree_sha` is `HEAD^{tree}`, so it describes the last commit rather than the
    files on disk and would be unchanged by a host that checked out the right
    commit and then edited it. What closes that gap is a precondition enforced on
    each side separately -- `_identity` in `conformance/tls/host.py` refuses to
    launch from a checkout with staged, unstaged or untracked changes, and
    :func:`_candidate` refuses to report from one -- so by the time these two
    pairs are compared, each is known to describe its own working state. Recording
    `candidate_identity_match` is the closeout's evidence that every other
    assertion in this file was made against *this* candidate.
    """
    operation = _required(OPERATION_VARIABLE)
    assert operation == ATTEST_OPERATION, (
        f"{OPERATION_VARIABLE} must be {ATTEST_OPERATION!r}: it is the one "
        f"operation the conformance host grants, and {operation!r} would mean the "
        "identity below came from a grant nobody approved"
    )
    assert _required(PURPOSE_VARIABLE) == "conformance"

    body = _request(operation, _required(PURPOSE_VARIABLE), reported["api_version"])
    status, _, response = _post(
        public_client, address, port, APPLICATION_PATH, body, credential=bearer
    )
    assert status == 200, f"the attestation operation answered {status}"
    attested = json.loads(response)["result"]
    candidate = _candidate()

    EVIDENCE["attestation"] = {
        "operation": operation,
        "host": {key: attested.get(key) for key in ("commit_sha", "tree_sha")},
        "runner": candidate,
    }
    EVIDENCE["candidate_identity_match"] = (
        attested.get("commit_sha") == candidate["commit_sha"]
        and attested.get("tree_sha") == candidate["tree_sha"]
    )

    assert attested.get("commit_sha") == candidate["commit_sha"], (
        "the host is not serving the commit this run is certifying"
    )
    assert attested.get("tree_sha") == candidate["tree_sha"], (
        "the host's committed tree is not the committed tree this run is "
        "certifying; both checkouts were separately required to be clean, so this "
        "is a different candidate rather than a dirty one"
    )
    assert EVIDENCE["candidate_identity_match"] is True
    assert bearer not in json.dumps(EVIDENCE, default=str), (
        "the bearer credential reached the evidence artifact"
    )
