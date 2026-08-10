"""The conformance host launcher: one TLS listener, one operation, nothing else.

Why this file is tracked rather than pasted onto a droplet
----------------------------------------------------------
The hosted lane proves that *this checkout* served the handshake. A launcher that
lives only on the host cannot be part of that proof: nobody reviewing the
evidence can say what was running, and "the same artifact at the same commit"
becomes an assertion about a machine instead of a property of a tree. So the
launcher is in the tree, under `conformance/`, and the run it starts reports the
**committed** commit and tree it was launched from through an authenticated
operation. The runner then computes the same two values locally and requires them
to be equal. That is what makes `candidate_identity_match` a fact rather than a
hope.

`tree_sha` is `HEAD^{tree}` and therefore says nothing about the files on disk: a
checkout with edits sitting in it reports the same tree as a pristine one. So the
working state is a *separately enforced precondition* rather than something the
attested values carry -- :func:`_identity` refuses to return at all unless
`git status` reports the checkout clean, and the runner holds itself to the same
rule before comparing. Together those two refusals are what makes the pair of
SHAs describe the bytes being served; neither SHA proves it alone.

What it is allowed to be
-------------------------
An **approved embedder**, and only that. `omnivia_core_runtime.service.main`
supplies no credential resolver and exits 2 on every `--http-endpoint` bind by
design, and this file does not reach around that: it never imports the console
entry point, never constructs `Dispatcher`, and never calls
`build_service_registry`. It builds a :class:`DocumentRouter` over a dispatcher
of its own that answers exactly one operation, wires it into
:class:`HttpListener` with :class:`HttpTls`, and starts it. Nothing about
production serving is enabled by running this, which is the boundary a V06-4 GO
must not be read past and the reason the hosted suite still asserts the console
script's refusal.

The one operation
------------------
`conformance.attest`, for purpose `conformance`, granted to one principal by a
resolver this file owns. It returns the checkout's commit and tree and nothing
else -- no workspace contents, no configuration, no path. It is deliberately not
in the accepted application catalogue: a conformance operation a production
registry could serve would be a production capability, and this one has exactly
one implementation, here, reachable only from a listener started by this file.

Refusals, all before a socket exists
-------------------------------------
Every argument is checked, the identity is computed and every byte this launcher
reads is read *before* :meth:`start`, so a misconfigured host never binds at all
rather than binding and then failing one caller at a time. A loopback or
non-literal address, TLS material that is absent or unreadable, a bearer file
that anyone but its owner can read, a workspace that is not marked synthetic, a
checkout git cannot identify, or a checkout carrying staged, unstaged or
untracked changes: each is a refusal with nothing listening.

Each of those reads is sanitized down to the option name, because both ways a
read fails carry something this process must not publish: `OSError` quotes the
path it failed on, and `UnicodeDecodeError` carries the bytes it choked on --
which, for `--bearer-file`, are the credential. The refusals are built outside
the handler rather than with `from None`, which clears `__cause__` and leaves
`__context__` referencing the original one attribute access away.

The one thing that cannot be done first is the launch record, because it reports
the URL the socket actually bound. So :func:`launch` retires the listener if
either the bind or that write fails, and there is no state in which this port is
open and no evidence accounts for it.

What the evidence may say
--------------------------
The launch record carries the bind address and port, the identity, the leaf
fingerprint, the grant, and the versions this build reports. It carries no
credential, no key bytes and no filesystem path -- the option names are the only
thing a refusal names, for the same reason. The fingerprint is published because
it is what the runner needs in `OMNIVIA_TLS_CONFORMANCE_CHAIN_SHA256` to prove
nothing terminated TLS in between; it is a public value, presented to every peer
that connects.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import re
import signal
import ssl
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnivia_core_runtime.service.authorization import AuthenticatedSession
from omnivia_core_runtime.service.http_transport import (
    HttpBind,
    HttpListener,
    HttpTls,
    HttpTransportError,
)
from omnivia_core_runtime.service.operations import success
from omnivia_core_runtime.service.probes import ProbeRouter, ServiceFacts
from omnivia_core_runtime.service.protocol import DocumentRouter
from omnivia_core_runtime.service.versions import API_VERSION, SERVER_VERSION

from omnivia_core.contracts.v1 import RequestEnvelope, ResponseEnvelope

#: The whole grant. One operation, one purpose, one principal.
OPERATION = "conformance.attest"
PURPOSE = "conformance"
PRINCIPAL = "omnivia-tls-conformance"

#: The file that has to be in the workspace directory for it to count as
#: synthetic. A marker rather than an inspection of the contents: what makes a
#: workspace safe to expose is that whoever provisioned it said so deliberately,
#: and a heuristic over its files would be this launcher guessing on their
#: behalf.
SYNTHETIC_MARKER = "SYNTHETIC-CONFORMANCE-WORKSPACE"

#: The floor on the bearer. Short enough to type, long enough that the credential
#: is not the weak link in a listener reachable from the public internet.
MINIMUM_BEARER_LENGTH = 32

#: The one permitted mode on the bearer file. Exact rather than a mask: a
#: credential file that is group- or world-readable is the failure that matters,
#: and an executable or set-uid bit on one is a mistake worth refusing rather
#: than normalising away.
BEARER_FILE_MODE = 0o600

#: A resolved git object, spelled the one way `git rev-parse` spells it.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: The repository root this file was launched from: `conformance/tls/host.py`.
CHECKOUT = Path(__file__).resolve().parents[2]


class LaunchRefused(Exception):
    """The host could not be launched, and nothing was bound.

    The message names the **option**, never its value. A path, a credential or a
    key is either something the operator typed on their own command line -- so
    repeating it buys nothing -- or something this process must not publish.
    """


# --- what the checkout is ----------------------------------------------------


def _git(checkout: Path, *arguments: str) -> str | None:
    """`git` in `checkout`, or None if it could not run or refused.

    No `git` on this host at all folds into the same None as a non-zero exit, and
    is taken without the `FileNotFoundError`, which quotes what it was trying to
    run. Callers derive their refusal from *whether* this returned, never from
    what it returned: `git status` names paths, and no path may reach a caller.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return completed.stdout if completed.returncode == 0 else None


def _identity(checkout: Path = CHECKOUT) -> dict[str, str]:
    """The committed commit and tree this launcher was started from, from a clean
    checkout.

    Read before the listener binds, so a host that cannot say what it is never
    serves. Both values are required to be resolved object names: `rev-parse`
    echoes its argument back on failure, and an unresolved `HEAD` recorded as an
    identity would make the runner's equality check compare two spellings rather
    than two objects.

    Neither value describes the files on disk. `HEAD^{tree}` is the tree of the
    last commit, so a checkout with edits, staged changes or added files in it
    attests exactly what a pristine one does -- which would let this host serve
    something other than the candidate while reporting the candidate's SHAs. So
    cleanliness is enforced here, as a precondition of returning at all, with
    porcelain status and untracked files included. That is structural rather than
    a diff heuristic: anything git reports as a difference from `HEAD`, ignored
    files aside, is a difference.

    The refusal names no path and quotes no status line. What is dirty is on the
    operator's own console; publishing it from a process that is about to be
    reachable from the public internet is not this launcher's to do.
    """
    found: dict[str, str] = {}
    for label, revision in (("commit_sha", "HEAD"), ("tree_sha", "HEAD^{tree}")):
        resolved = _git(checkout, "rev-parse", revision)
        value = "" if resolved is None else resolved.strip()
        if _SHA_RE.fullmatch(value) is None:
            raise LaunchRefused(
                "this checkout has no readable git identity, so the host cannot "
                "attest the commit and tree it is serving"
            )
        found[label] = value

    status = _git(checkout, "status", "--porcelain", "--untracked-files=all")
    if status is None:
        raise LaunchRefused(
            "this checkout's working state could not be read, so the host cannot "
            "attest that what it serves is the committed tree"
        )
    if status.strip():
        raise LaunchRefused(
            "this checkout has staged, unstaged or untracked changes, so the "
            "committed tree it would attest is not what it is serving; commit or "
            "clean the checkout before launching"
        )
    return found


# --- what the operator supplied ----------------------------------------------


def _address(value: str) -> str:
    """A non-loopback IP literal, or a refusal.

    A hostname is refused rather than resolved -- `HttpBind` refuses one too, and
    for the same reason: what a name resolves to is host configuration. Loopback
    is refused here rather than there because `HttpBind` admits it, and a
    loopback conformance host proves nothing about a non-loopback listener.
    """
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        parsed = None
    if parsed is None:
        raise LaunchRefused("--address must be an IP literal, not a name")
    if parsed.is_loopback:
        raise LaunchRefused(
            "--address must not be a loopback address: the hosted lane exists to "
            "prove a non-loopback TLS listener"
        )
    return str(parsed)


def _port(value: str) -> int:
    if not value.isascii() or not value.isdigit():
        raise LaunchRefused("--port must be a decimal port number")
    port = int(value)
    if not 1 <= port <= 65535:
        raise LaunchRefused("--port is out of range")
    return port


def _readable_file(path: Path, option: str) -> Path:
    resolved = path.expanduser()
    if not resolved.is_file():
        raise LaunchRefused(f"{option} does not name a readable file")
    failed = False
    try:
        with resolved.open("rb") as handle:
            handle.read(1)
    except OSError:
        failed = True
    if failed:
        raise LaunchRefused(f"{option} could not be read")
    return resolved


def _text(path: Path, option: str) -> str:
    """The whole file as text, or a refusal that names only the option.

    Every read of caller-supplied material goes through here. An `OSError` quotes
    the path it failed on and a `UnicodeDecodeError` carries the bytes it choked
    on in `args` -- which for `--bearer-file` are the credential this launcher
    exists to protect -- so neither may reach a caller, and the refusal is built
    outside the handler so neither survives on `__context__` either.

    Strict UTF-8 rather than `errors="replace"`. Everything this launcher reads
    is text by definition: a PEM chain, a PEM key, a marker, a credential. A file
    that will not decode is a misconfiguration worth refusing while nothing is
    listening, rather than a string to approximate and carry on with.
    """
    content: str | None = None
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        content = None
    if content is None:
        raise LaunchRefused(f"{option} could not be read as UTF-8 text")
    return content


def _bearer(path: Path) -> str:
    """The credential this host will accept, from a file only its owner can read.

    A bearer that grants an operation on a publicly reachable listener is not a
    configuration value, so the mode is :data:`BEARER_FILE_MODE` exactly.
    """
    resolved = _readable_file(path, "--bearer-file")
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode != BEARER_FILE_MODE:
        raise LaunchRefused(
            f"--bearer-file must be mode {BEARER_FILE_MODE:04o}; a credential "
            f"readable by anyone else is not usable here (found {mode:04o})"
        )
    credential = _text(resolved, "--bearer-file").strip()
    if not credential:
        raise LaunchRefused("--bearer-file is empty")
    if len(credential) < MINIMUM_BEARER_LENGTH:
        raise LaunchRefused(
            f"--bearer-file holds fewer than {MINIMUM_BEARER_LENGTH} characters"
        )
    if any(character.isspace() for character in credential):
        raise LaunchRefused("--bearer-file holds whitespace inside the credential")
    return credential


def _workspace(path: Path) -> str:
    """The synthetic workspace's id, or a refusal.

    The directory has to carry :data:`SYNTHETIC_MARKER`, and the id is the
    marker's own contents rather than the directory name: a path component is a
    filesystem layout, and this value is echoed into evidence and into every
    session this host resolves.
    """
    resolved = path.expanduser()
    if not resolved.is_dir():
        raise LaunchRefused("--workspace does not name a directory")
    marker = resolved / SYNTHETIC_MARKER
    if not marker.is_file():
        raise LaunchRefused(
            f"--workspace holds no {SYNTHETIC_MARKER} marker, so it is not a "
            "workspace this host may expose"
        )
    workspace_id = _text(marker, f"--workspace's {SYNTHETIC_MARKER} marker").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", workspace_id or ""):
        raise LaunchRefused(
            f"--workspace's {SYNTHETIC_MARKER} marker does not name a workspace id"
        )
    return workspace_id


def _leaf_fingerprint(chain: Path) -> str:
    """SHA-256 of the DER of the first certificate in the configured chain.

    This is what the runner compares the leaf it was handed on the wire against,
    so it is computed from the *configured* file rather than from a connection to
    this listener -- a fingerprint read back through a proxy would agree with the
    proxy.

    The chain's `iPAddress` SAN is deliberately **not** checked here. `ssl` offers
    no route to a leaf's parsed extensions -- `get_ca_certs` reads a trust store
    and drops a `CA:FALSE` certificate, which every real leaf is -- and adding a
    dependency for certificate parsing is prohibited. The suite proves the SAN
    from the wire instead, which is the stronger claim: it is about what the
    listener presented, not about what a file on the host said.
    """
    text = _text(chain, "--certificate-chain")
    marker = "-----END CERTIFICATE-----"
    head, separator, _ = text.partition(marker)
    if not separator:
        raise LaunchRefused("--certificate-chain holds no PEM certificate")
    der: bytes | None = None
    try:
        der = ssl.PEM_cert_to_DER_cert(head + marker + "\n")
    except ValueError:
        der = None
    if der is None:
        raise LaunchRefused("--certificate-chain is not readable PEM")
    return hashlib.sha256(der).hexdigest()


# --- the one operation -------------------------------------------------------


class Attestation:
    """Application dispatch for this host: one operation, answered from memory.

    A plain callable rather than anything from `dispatch.py`: `Dispatcher` runs
    the accepted service registry against a real workspace, which is a production
    surface, and this endpoint is entitled to none of it. What it returns is the
    two values computed before the bind, so answering the operation reads nothing
    -- no file, no process state, no git invocation with a caller waiting on it.
    """

    def __init__(self, identity: Mapping[str, str]) -> None:
        self.identity = dict(identity)

    def __call__(self, request: RequestEnvelope) -> ResponseEnvelope:
        if request.operation != OPERATION:
            # Unreachable through the adapter -- `_session_admits` refuses any
            # other operation with 403 before dispatch is called -- and kept
            # anyway, so "this dispatcher serves one operation" is true of the
            # dispatcher rather than only of the session in front of it.
            raise LaunchRefused("this host dispatches only the conformance operation")
        return success(request, dict(self.identity), principal=PRINCIPAL)


def resolver(
    credential: str, *, bearer: str, workspace_id: str
) -> AuthenticatedSession | None:
    """The harness-owned credential resolver. One credential, one session.

    Compared with :func:`hmac.compare_digest` so a wrong credential takes the
    same time as a right one; a resolver on a publicly reachable listener that
    short-circuits on the first differing byte is a timing oracle for the value
    it protects. A non-ASCII credential makes `compare_digest` raise rather than
    answer, which the adapter's `_resolved` contains as "no session" -- the same
    outcome, reached without this function inspecting the caller's bytes to
    decide how to refuse.

    The session it returns is the whole grant: one operation, one purpose, one
    synthetic workspace, no scopes and no roles. Everything a request may claim
    is therefore either that or a widening, which is what makes the suite's
    refusal cases attributable rather than incidental.
    """
    if not hmac.compare_digest(credential, bearer):
        return None
    return AuthenticatedSession(
        principal_id=PRINCIPAL,
        workspaces=frozenset({workspace_id}),
        operations=frozenset({OPERATION}),
        purposes=frozenset({PURPOSE}),
    )


def _router(identity: Mapping[str, str]) -> DocumentRouter:
    """The router behind the listener: this host's probes and this host's one op."""

    def facts() -> ServiceFacts:
        return ServiceFacts(
            observed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            health_status="pass",
            readiness_status="pass",
            discovery_status="pass",
        )

    return DocumentRouter(
        probes=ProbeRouter(facts=facts, capabilities=tuple, clock=time.monotonic_ns),
        dispatch=Attestation(identity),
    )


# --- launching ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnivia-core-tls-conformance-host",
        description=(
            "Start the V06-4 conformance TLS listener. Not a service entry point: "
            "it serves one operation to one credential and is used by nothing else."
        ),
    )
    parser.add_argument("--address", required=True, help="non-loopback IP literal")
    parser.add_argument("--port", required=True, help="the one open TCP port")
    parser.add_argument("--certificate-chain", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--bearer-file", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    return parser


def launch(arguments: Sequence[str]) -> tuple[HttpListener, dict[str, Any]]:
    """Check everything, bind, and return the listener and its launch record.

    Ordered so that nothing is listening until every refusal has had its chance:
    the arguments, then the credential, then the identity, then the TLS material,
    then the withheld set -- which is a full read of the private key -- and only
    then :meth:`HttpListener.start`.

    The record itself is the one thing that cannot be built first, because it
    reports the URL the socket actually bound. So the bind and the write are one
    unit: if either fails the listener is retired on the way out, and this
    function never returns, and never leaves behind, a bound port with no
    evidence describing it.
    """
    parsed = build_parser().parse_args(arguments)
    address = _address(parsed.address)
    port = _port(parsed.port)
    chain = _readable_file(parsed.certificate_chain, "--certificate-chain")
    key = _readable_file(parsed.private_key, "--private-key")
    workspace_id = _workspace(parsed.workspace)
    bearer = _bearer(parsed.bearer_file)
    identity = _identity()
    fingerprint = _leaf_fingerprint(chain)

    # Computed here rather than beside the write below, because it is a full read
    # of the private key and every byte this launcher reads is read before the
    # socket exists. A key that became unreadable between the bind and the write
    # would otherwise leave a listener up with nothing to check the record
    # against.
    withheld = (
        bearer,
        _text(key, "--private-key"),
        # Resolved rather than as typed. A relative `--workspace ws` withheld as
        # `"ws"` is a substring of almost any workspace id and would refuse a
        # correctly configured host. What must stay out of the record is the
        # location on this filesystem, which is the absolute path -- and that is
        # also the spelling a leak would carry.
        *(str(supplied.resolve()) for supplied in (chain, key, parsed.workspace)),
    )

    listener = HttpListener(
        router=_router(identity),
        principal=PRINCIPAL,
        resolver=lambda credential: resolver(
            credential, bearer=bearer, workspace_id=workspace_id
        ),
        bind=HttpBind(
            host=address,
            port=port,
            tls=HttpTls(certificate_chain=chain, private_key=key),
        ),
    )
    try:
        listener.start()
        record = {
            "bind": {"address": address, "port": port, "url": listener.url},
            "identity": identity,
            "leaf_sha256": fingerprint,
            "grant": {
                "principal": PRINCIPAL,
                "operation": OPERATION,
                "purpose": PURPOSE,
                "workspace_id": workspace_id,
                "scopes": [],
            },
            "versions": {"api_version": API_VERSION, "server_version": SERVER_VERSION},
            "launched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "synthetic_workspace": True,
        }
        _write_evidence(parsed.evidence, record, withheld=withheld)
    except BaseException:
        # `stop()` returns immediately when nothing was bound, so this is correct
        # whether `start()` failed before the socket, the record was refused for
        # carrying a withheld value, or the write itself failed. `BaseException`
        # rather than `Exception`: a signal arriving in this window would
        # otherwise leave a publicly reachable port open behind a dying process.
        listener.stop()
        raise
    return listener, record


def _write_evidence(
    path: Path, record: Mapping[str, Any], *, withheld: Sequence[str]
) -> None:
    """Write the launch record, after proving it carries nothing withheld.

    Asserted on the serialized bytes rather than field by field, because the
    thing that must not happen is a value *reaching the file*, and a per-field
    check only covers the fields someone remembered to check. The withheld set is
    the credential, the private key's own bytes, and every path this launcher was
    handed.
    """
    document = json.dumps(dict(record), indent=2, sort_keys=True) + "\n"
    for value in withheld:
        if value and value in document:
            raise LaunchRefused("the launch record would carry a withheld value")
    destination = path.expanduser()
    failed = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(document, encoding="utf-8")
    except OSError:
        failed = True
    if failed:
        # The option name and nothing else: an unwritable destination is the one
        # place a path would otherwise reach a caller through an `OSError`, and
        # the refusal is built here rather than in the handler so the original
        # is not left on `__context__`. `launch` retires the listener on it.
        raise LaunchRefused("--evidence could not be written")


def main(arguments: Sequence[str] | None = None) -> int:
    """Launch, report what a runner needs, and serve until signalled.

    The three values printed are what the workflow environment needs and none is
    a secret: the fingerprint is presented to every peer that connects, and the
    commit is what this run exists to attest. The bearer is never printed, and
    neither is any path.

    Nothing reaches a terminal as a traceback. :class:`LaunchRefused` and
    `HttpTransportError` both state the refusal without naming a value, so their
    own text is printed. Anything else that can still come out of `launch` is an
    `OSError` from material this launcher inspects but does not read -- and an
    `OSError` names the path it failed on, so that branch prints a fixed sentence
    instead of the exception. In every case `launch` has already retired the
    listener, so a non-zero exit here means nothing is bound.
    """
    try:
        listener, record = launch(sys.argv[1:] if arguments is None else arguments)
    except (LaunchRefused, HttpTransportError) as refusal:
        print(f"conformance host refused to launch: {refusal}", file=sys.stderr)
        return 2
    except OSError:
        print(
            "conformance host refused to launch: the supplied material could not "
            "be inspected",
            file=sys.stderr,
        )
        return 2

    print(f"listening on {record['bind']['url']}")
    print(f"OMNIVIA_TLS_CONFORMANCE_CHAIN_SHA256={record['leaf_sha256']}")
    print(f"attesting commit {record['identity']['commit_sha']}")

    stopping = threading.Event()
    for received in (signal.SIGINT, signal.SIGTERM):
        signal.signal(received, lambda *_signal: stopping.set())
    try:
        stopping.wait()
    finally:
        # `stop()` shuts the serving thread down and closes the socket. Run from
        # `finally` so an unexpected exception in the wait still retires the
        # listener rather than leaving a public port open behind a dead process.
        listener.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
