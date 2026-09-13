# Shared Core installation

The per-user macOS installation root is
`~/Library/Application Support/OmniVia/Core`. Immutable runtime payloads live
under `runtimes/<semver>/<sha256>/`; the canonical companion is
`~/Applications/OmniVia Core.app` with bundle identifier
`com.omnivia.core.status`.

Standalone Core and OmniVia Platform are independent consumers. Each writes an
owner-only JSON receipt containing its stable identity, installed consumer
payload digest and minimum compatible Core version. The Core-owned installation
manager selects the highest compatible installed runtime deterministically and
updates `active.json` atomically. The prior selection is retained as
`previous-known-good.json`.

Removing one consumer removes only that receipt. It never removes Workspace
data, the active runtime, the previous known-good runtime, a payload another
consumer references, or the companion while another consumer remains. Garbage
collection is a separate explicit operation; the manager reports eligible
payloads but does not delete them implicitly.

The companion is on-demand only. It uses an owner-only lock and Unix activation
socket below the explicit installation state. A second launch forwards only the
fixed `refresh` intent and exits. Quitting it performs no Core lifecycle action.
No login item or LaunchAgent is installed by this programme.

## Trusted runtime payloads

Selection above is deterministic. It is not trust: it decides *which* payload,
never whether that payload is the one a release signed. Runtime payload v1 adds
that, and the contract is language-neutral so a consumer can verify a Core
runtime without running Core.

- Schema: `contracts/runtime/v1/schemas/trusted-runtime-v1.schema.json`
  (`$id` `https://contracts.omnivia.dev/runtime/v1/trusted-runtime.schema.json`).
- Conformance corpus and vectors: `contracts/runtime/v1/fixtures/{valid,invalid,vectors}`.
- Packaged for consumers as `omnivia_core.runtime_contract.v1.resources` in the
  `omnivia-core` wheel; regenerated and gated by
  `python scripts/generate-runtime-contract.py [--check]`.
- Reference verifier: `omnivia_core_runtime.distribution.trusted_runtime`.

### What a payload is

A candidate directory at `runtimes/<semver>/<payload-identity>/` holding

- `omnivia-runtime-manifest.json` — the manifest;
- `omnivia-runtime-manifest.sig.json` — the detached release signature; and
- every inventoried file, and nothing else.

The two metadata documents are deliberately outside the inventory: a manifest
cannot contain its own digest, and both are covered by the signature over the
manifest instead. Any file present that the inventory does not declare is a
refusal, not a file to ignore.

### Identity, and why it is not a self-referential hash

`payload_identity` is `sha256` of a domain-separated canonical identity claim
over every manifest member **except** `payload_identity`. The detached signature
covers a *second* domain-separated canonicalisation of the manifest **including**
the computed identity, so a signature cannot be lifted onto a manifest whose
identity differs.

```text
identity  = sha256("omnivia.runtime-payload-identity.v1"  || 0x00 || canonical(manifest - payload_identity))
signed    =        "omnivia.runtime-payload-signature.v1" || 0x00 || canonical(manifest + payload_identity)
```

The manifest declares its own identity so the document is self-describing. The
declared value is always recomputed and compared; **neither digest is ever caller
authority**, and a caller-supplied digest is at most an equality guard.

The canonical form is a strict ASCII/UTF-8 JSON subset: object names sorted by
their UTF-8 bytes, no insignificant whitespace, arrays in contract-defined order,
integers only — **no floats** — and duplicate names refused on parse. Inventory
paths are unique normalised relative POSIX paths sorted by UTF-8 bytes.
`contracts/runtime/v1/fixtures/vectors/canonicalisation-and-signatures.json`
carries the vectors that pin all of it, as raw JSON text rather than parsed
values, because a parsed value cannot carry a duplicate name and a language whose
only number is a double cannot tell `1` from `1.0`.

### Release signatures and key rotation

Ed25519, and no other algorithm in v1. Trust anchors are **explicit resolver
inputs**, never read from the installation being verified: a payload that could
nominate the key which verifies it is self-certifying. Each anchor carries a
`key_id`, the public key, a `not_before`/`not_after` window and an optional
`retired_at`.

A key verifies a release only while `not_before <= t < not_after` and
`retired_at` is unset or still in the future. Rotation is expressed entirely by
overlapping windows, so a transition needs no flag day. Unknown, not-yet-valid,
expired and retired keys are one answer to a consumer — `runtime_untrusted` —
because they are one fact: this release was not issued by anybody currently
approved to issue one.

The keys in the conformance corpus are **test-only**, published, and regenerable
from labels in `scripts/generate-runtime-contract.py`. They have no production
role. No release signing key, private or otherwise, is held in this repository;
a release is signed off-repository and only its public half reaches an anchor.

### Production signing and key lifecycle

`scripts/sign-runtime-payload.py` is the release packaging boundary for a
prepared runtime directory. It inventories the complete payload, emits the two
canonical v1 metadata documents, signs with Ed25519, and verifies its own output
with the reference verifier before it succeeds. The private key is accepted only
as an explicit absolute path to a raw 32-byte seed; it is never accepted on argv
as bytes, read from the repository, printed, or copied into the payload. On POSIX
the materialized key file must be owner-only.

Production release jobs must materialize that file from the release secret store
or HSM-backed signing workflow for the duration of the job and remove it after
the command exits. Production key IDs are stable release identities (for example,
`omnivia-release-2026a`), not payload versions. Rotation publishes the incoming
public anchor before its first signed release and overlaps the outgoing and
incoming validity windows. Emergency revocation sets `retired_at` on the affected
public anchor and distributes the updated Platform trust-anchor set; a verifier
then refuses the key at and after that instant even if `not_after` is later.

The command prints a JSON result containing the computed payload identity and the
public trust anchor. That anchor is a release input for Platform packaging. The
repository intentionally contains neither a production private key nor a
fabricated production public anchor: provisioning the real release identity is
an external release-ownership action, while the mechanism and lifecycle policy
are fixed here.

### Fixed executable layout

| Operating system | CLI | Service |
| --- | --- | --- |
| macOS, Linux | `bin/omnivia` | `bin/omnivia-core-service` |
| Windows | `Scripts/omnivia.exe` | `Scripts/omnivia-core-service.exe` |

The manifest restates these paths so the document is self-describing and may not
name any other value. Metadata supplies no argv, no environment, no working
directory and no command: this is not a signed-command framework, and an unknown
manifest member is refused rather than ignored.

### Host compatibility: both halves of `platform`

The manifest declares `platform.os` and `platform.arch` (`arm64` or `x86_64`),
and **both are compared with the host**. The resolver takes each as an explicit
argument — `host_operating_system` and `host_architecture` — defaulting to
`HOST_OPERATING_SYSTEM` and `HOST_ARCHITECTURE`, which normalise what the host
reports (`aarch64` → `arm64`, `AMD64` → `x86_64`) into the manifest's spelling. A
machine name outside that table passes through unchanged and therefore matches
neither, which refuses rather than guesses.

A mismatch on either half is `runtime_incompatible`: the payload is authentic and
is for another machine, so the remedy is to install a compatible Core rather than
to repair this one. Both are **compatibility inputs and never trust inputs** —
passing the wrong one cannot make an unsigned payload verify — and they are
explicit precisely so one conformance corpus produces the same verdicts on every
supported operating system and architecture. Every case states both.

### Document character policies

Two of the schema's definitions are character policies rather than shapes, and
the reference verifier restates them exactly rather than approximately:

| Definition | Policy | Used by |
| --- | --- | --- |
| `keyId` | `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` | `signing.key_id`, the detached signature, a trust anchor |
| `relativePath` | 1–512 characters, no leading `/`, no empty/`.`/`..` component, and none of `\ : * ? " < > |` or any character below U+0020 | inventory and executable paths, `relative_path`, conformance entries |

The direction that matters is that a verifier must never be *looser* than the
schema: a document the schema rejects but a verifier accepts is a document one
implementation runs and the other refuses, which is the disagreement the corpus
exists to prevent, and which no corpus *case* can catch — a case can only carry
documents the schema already admits. `tests/runtime_contract/test_runtime_conformance.py`
asserts both policies against the published `$defs` themselves.

### Per-user installation policy

Runtime payload v1 is a per-user installation. On POSIX, after installation every
component of a candidate is owned by the effective user and writable by nobody;
ordinary files carry no execute bit and exactly the two product executables are
owner-executable. On Windows the reparse-point and regular-file checks apply and
the installed files are marked read-only; NTFS has no mode bits and enforcing an
emulation of them would report a property nobody has.

**This is defence in depth and is not the trust root.** The same user can
`chmod` any of it back. What makes tampering unusable is that the signature and
every digest are reverified on every resolver call; the mode policy raises the
cost of a partial write and closes the accidental cases.

**Residual, stated rather than papered over.** The Ed25519 release signature plus
the full inventory is the v1 trust root on every supported operating system.
Operating-system package or code identity — an Apple Team ID and designated
requirement, an Authenticode signer — is an *additional* product policy that v1
neither verifies nor asserts. Nothing here gathers that evidence, and nothing
here claims to.

### Resolution

`resolve_runtime()` takes an explicit absolute installation root, explicit
approved trust anchors, an explicit verification instant and the consumer's
compatibility bounds. It never reads `HOME`, consults `PATH`, runs `which`,
searches recursively, opens a socket or executes a byte of the payload.

`active.json` is read as a **selector and nothing more**: its release version and
payload digest are used, the candidate directory is derived beneath the explicit
root, and its `relative_path` is checked against that derived value rather than
supplying one. Everything is redone on every call — a returned descriptor is a
cache hint for the consumer and never continuing authority.

Success returns the verified descriptor and nothing else:

```json
{
  "runtime_descriptor_version": "1.0",
  "release_version": "0.6.5",
  "payload_identity": "sha256:<64 lowercase hex>",
  "runtime_root": "/absolute/canonical/runtime/root",
  "cli_path": "/absolute/canonical/runtime/root/bin/omnivia",
  "service_path": "/absolute/canonical/runtime/root/bin/omnivia-core-service"
}
```

### Refusal vocabulary

A failed resolution answers with one closed code and a version, and nothing
else. That is a ceiling on the exfiltration surface rather than terseness: with
no message, no path and no offending value in the document, no file content, key
material or absolute payload path can leave through it. Human diagnostics stay on
stderr.

| Code | Meaning | Retry posture |
| --- | --- | --- |
| `runtime_not_installed` | No active installed candidate exists | Retry after installation or repair |
| `runtime_metadata_invalid` | Selection or manifest malformed, oversized or unsupported | Repair required |
| `runtime_untrusted` | Signature, key or key window not approved | Never execute; repair required |
| `runtime_tampered` | Payload identity or a file digest does not match | Never execute; repair required |
| `runtime_layout_invalid` | Pair missing, split, escaping, symlinked, wrong mode or owner | Never execute; repair required |
| `runtime_incompatible` | Authentic release cannot serve the consumer contract | Install a compatible Core |
| `runtime_busy` | Installation selection is being changed | Bounded retry |
| `runtime_io_failure` | A bounded local read failed | Bounded retry or repair |

These are distinct from the already-versioned `WorkspaceInitRefusal` and
`ManagedStartFailure` vocabularies and are never collapsed into them.

### Installation

`SharedRuntimeInstallation.install_candidate()` verifies the staged payload and
**computes** the release version and payload identity from the signed manifest,
then places the tree at `runtimes/<computed version>/<computed identity>/`. The
legacy `release_version` and `payload_digest` arguments remain as optional
equality guards for callers that already know what they are installing; a
mismatch is a refusal and neither is ever the computed value.

The order is verify, move, harden, publish the index. A candidate is selectable
only through `candidates/<identity>.json`, so the tree is unreachable until that
last step; hardening cannot precede the move because renaming a directory into a
new parent requires write permission on the directory being moved.

**Nothing already at the destination is adopted on the strength of its name.**
The directory name is derived from content, which is a *claim* about those bytes
and not proof of them. When the atomic no-clobber move finds something there — a published
candidate being reinstalled, another installer of the same identity that won the
race, or the unindexed tree a crash or a failed publication left behind — that
tree is put through the full verifier against the identity and release this call
computed, before it is hardened, adopted or indexed. A tree that fails is
`distribution candidate conflict`, and it is **neither overwritten nor removed**:
overwriting destroys the only evidence of what happened, and deleting races
whichever installer may be publishing it. A published index that names a
different record for the same identity is the same refusal.

**A failure removes this call's staging tree and nothing else.** The moved
destination is deliberately not cleaned up: the candidate index is shared, so
another installer of the same identity converges on exactly that tree and
publishes it, and deleting it on the way out of a local failure would remove a
payload another process had already told its consumers about. What is left
instead is an unindexed tree whose contents were verified, which the next
installation reconciles by writing the record that is missing. Write permission
is restored before any removal, because a hardened directory admits no unlink.

### Bootstrap seam

The bootstrap contract version a payload can serve is declared in the manifest's
`compatibility` window and stated by the consumer on every call. Version `1.0`
is the existing machine-readable `omnivia-core-service --init` and
`--managed-start` documents, unchanged by this contract. Invoking the verified
`service_path` by absolute path is what binds the child too: managed start
selects `sys.argv[0]` before `PATH`, so the service it spawns is the same
verified executable. That property is pinned by
`test_the_exact_invoked_service_spawns_itself_and_never_a_path_substitute`
against a hostile `omnivia-core-service` placed first on `PATH`.

A consumer that selects a workspace from the filesystem should additionally
pass `--expected-manifest-digest` over the exact selected `workspace.json` bytes.
For legacy fallback it should pass the preferred registered manifest path as
`--required-absent-manifest`. Managed start validates and propagates both to the
service, so the manifest and precedence decision are consumed as one snapshot
rather than repeated from mutable pathnames.

The other half of the seam is that a ready answer is about the workspace the
caller selected. Managed start dials `core.readiness` with the *expected*
workspace id rather than the advertised descriptor's own claim, and refuses a
descriptor whose id is not that one. Sending the descriptor's claim was
self-consistent and proved nothing: a second workspace's real, ready service
advertised in this workspace's runtime directory answered `ready` for the
workspace it named, and the launcher reported `attached`. Pinned by
`test_a_service_answering_for_another_workspace_is_not_a_successful_start`.

`--init` states the same contract on the way in: stdout is the versioned document
and the whole of it, stderr is the human sentence, and a refusal exits non-zero --
so success is read from `status`, never inferred from an exit code alone.
`test_a_refused_init_is_a_versioned_document_and_a_non_zero_exit` pins that
through the shipped console script rather than through an in-process call, which
cannot have an exit code to pin.
