# OmniVia Core trusted runtime and Workspace bootstrap — Core handback

Date: 2026-09-10

Status: Core contract, release payload emitter and signing lifecycle implemented;
actual production key provisioning remains release-owned; Platform lane not started

Answers: `docs/development/omnivia-core-trusted-runtime-workspace-bootstrap-platform-handoff-2026-09-10.md`
sections 4, 5, 6, 7 and 11. That document's steps 1–7 are what this note hands
back; steps 8–9 remain a separate Platform task and are not in this repository.

This note names what Platform consumes. It does not restate the design — that
lives in `docs/distribution/shared-core-installation.md` under
**Trusted runtime payloads**, which is the reviewed reference and the thing to
read before implementing the TypeScript verifier.

## 1. Contract files Platform consumes

| Path | What it is |
| --- | --- |
| `contracts/runtime/v1/schemas/trusted-runtime-v1.schema.json` | The one schema. `$id` `https://contracts.omnivia.dev/runtime/v1/trusted-runtime.schema.json`, JSON Schema draft 2020-12. Defines the payload manifest, the detached signature, the active-selection record, the trust anchor, the runtime descriptor and the conformance-case envelope. |
| `contracts/runtime/v1/fixtures/valid/` | 3 cases that must verify. |
| `contracts/runtime/v1/fixtures/invalid/` | 26 cases that must refuse, each carrying the exact refusal code. |
| `contracts/runtime/v1/fixtures/vectors/canonicalisation-and-signatures.json` | 11 canonicalisation vectors, 1 identity vector and 3 signature vectors, as **raw JSON text** rather than parsed values. |

Every case is self-describing: it carries the installation tree to materialise,
the approved anchors, the verification instant, the consumer's bounds, the host it
resolves as, and its own `expected.outcome` / `expected.refusal`. There is no
expectations table to keep in step with it, which is what makes "the two verifiers
agree" a checkable claim rather than a shared reading.

**A case states the host on both axes.** `host_operating_system` and
`host_architecture` are required members, and a verifier must resolve as the case
says rather than as the machine running it happens to be — that is what lets the
one corpus produce identical verdicts on an arm64 Mac, an x86_64 Linux runner and
a Windows runner. `invalid/wrong-payload-architecture.json` is the case that
proves the second one is compared: a correctly signed `x86_64` payload resolved by
an `arm64` host is `runtime_incompatible`, and a verifier that parses
`platform.arch` without comparing it verifies that case and fails the corpus.

**Consume these through the packaged copy, not through a path into this
checkout.** The `omnivia-core` wheel force-includes both directories and
`omnivia_core.runtime_contract.v1.resources` is the supported reader
(`list_schema_names`, `read_schema`, `read_case_text`, `read_vectors`, …). The
canonical source is the `contracts/runtime/v1` tree above;
`scripts/generate-runtime-contract.py` derives it and `--check` is a gate on both
`Core acceptance` and `./scripts/preflight`, so a hand-edited fixture fails the
build rather than quietly ceasing to test what it is named for.

## 2. Generated artifacts and how to regenerate them

```text
python scripts/generate-runtime-contract.py            # rewrite the corpus
python scripts/generate-runtime-contract.py --check    # gate: committed == generated
```

Every digest, payload identity and signature in the corpus is derived from
payloads the generator builds in memory. Nothing there was typed by hand and
nothing there should be.

Reference verifier: `omnivia_core_runtime.distribution.trusted_runtime`
(`resolve_runtime`, `verify_payload`, `payload_identity`,
`signed_manifest_bytes`, `canonical_json`, `TrustAnchor`, `VerifiedRuntime`,
`RuntimeRefusal`). Platform imports **none** of this; it is the thing the
TypeScript verifier is checked against, via the corpus.

## 3. Release keys and key IDs

The corpus is signed by two **test-only** Ed25519 keys, published here because a
conformance corpus nobody can verify is not one:

| Key ID | Public key (base64, raw Ed25519) | Role in the corpus |
| --- | --- | --- |
| `test-only-release-2026a` | `45TtObJ05zYKRBt3T+XUxwYbt7j1E/ky6M7QuwJi3SY=` | The approved key for every case except the rotation one; also the *retired*, *expired* and *not-yet-valid* subjects, under different windows |
| `test-only-release-2026b` | `mMKrJKdsS69CdtzbrBx/qE6A72JRrgfQS/CvGLiAbak=` | The incoming key in `valid/valid-key-rotation.json`, and the unapproved issuer in `invalid/unknown-signing-key.json` |

Both seeds are digests of published labels in
`scripts/generate-runtime-contract.py` and are reproducible from it. **They have
no production role.**

**No production release key exists yet, public or private, and none is held in
this repository.** The mechanism and lifecycle decision are complete:
`scripts/sign-runtime-payload.py` accepts only an external owner-only raw Ed25519
seed file, emits the canonical manifest/signature pair, self-verifies it, and
returns the public anchor. Release jobs materialize the private key from secret
storage or an HSM-backed workflow; stable key IDs and overlapping validity
windows provide rotation, and an updated anchor with `retired_at` provides
emergency revocation. Platform takes the resulting public anchors as explicit
packaging inputs. Provisioning the actual production key remains an external
release-ownership action and cannot safely be fabricated in source control.

## 4. Versions Platform must pin

| Contract | Version | Where it appears |
| --- | --- | --- |
| Payload manifest | `1.0` | `manifest_version` |
| Detached release signature | `1.0` | `signature_version` |
| Runtime descriptor | `1.0` | `runtime_descriptor_version`, on success **and** on every refusal |
| Bootstrap contract | `1.0` | the manifest's `compatibility` window; stated by the consumer on every resolve |
| Workspace init document | `1.1` | `workspace_init_version` on `--init` stdout |
| Managed start document | `1.0` | `managed_start_version` on `--managed-start` stdout |

Signature algorithm: `ed25519`, and no other in v1.

Host compatibility is compared on **both** halves of the manifest's `platform`.
The two resolver inputs default to the host, normalising what it reports
(`aarch64` → `arm64`, `AMD64` → `x86_64`) into the manifest's spelling; a machine
name outside that table matches neither and refuses. A mismatch on either half is
`runtime_incompatible`, never a trust code — the payload is authentic and is for
another machine.

Two schema definitions are character policies Platform's verifier must apply
verbatim rather than approximately, because a verifier looser than the schema
runs documents the other one refuses: `keyId`
(`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`) and `relativePath` (1–512 characters, no
leading `/`, no empty/`.`/`..` component, and none of `\ : * ? " < > |` or any
character below U+0020).

**Minimum compatible Core release: the release that first ships a signed runtime
payload manifest.** There is no earlier one — every Core distribution in this
repository is `version = "0.1.0"`, Core has never released, and no tag exists
(see `CHANGELOG.md`). A payload without `omnivia-runtime-manifest.json` is not an
older trusted runtime, it is an untrusted directory, and the resolver answers
`runtime_metadata_invalid` rather than falling back. Platform should express its
floor as `bootstrap_contract_version = "1.0"` plus, once release numbering
exists, `minimum_release_version`; both are already resolver inputs.

## 5. The seam, in the order Platform calls it

```text
resolve_runtime(installation_root=<absolute>, trust_anchors=[...],
                verification_time=<aware UTC>, bootstrap_contract_version="1.0",
                host_operating_system=<macos|linux|windows>,
                host_architecture=<arm64|x86_64>)
  -> {runtime_descriptor_version, release_version, payload_identity,
      runtime_root, cli_path, service_path}

<service_path> --init --workspace <folder> --installation-state <state>
               [--core-version <release_version>]
  -> stdout: the whole workspace-init document; workspace.workspace_id is authority
  -> exit 0 on `initialised` and on `already_initialised`; 1 on `refused`

<same service_path> --managed-start --workspace <same folder>
               --installation-state <same state> --endpoint <local endpoint>
               [--expected-manifest-digest <sha256:...>]
               [--required-absent-manifest <preferred manifest path>]
               [--core-version <same release_version>]
  -> stdout: the whole managed-start document; service.ready and
     service.workspace_id are authority, and the id must equal init's
```

Three properties worth knowing before writing the adapter:

- **A workspace selection can be carried across both process boundaries.** Pass
  `--expected-manifest-digest` over the selected `workspace.json` bytes. When a
  legacy path won only because the registered path was absent, also pass that
  preferred manifest as `--required-absent-manifest`. The launcher checks both
  and forwards both to the service's first manifest read.

- **Invoking the verified absolute path binds the child too.** Managed start
  selects `sys.argv[0]` before `PATH`, so the service it spawns is the same
  verified executable. Pinned by
  `test_the_exact_invoked_service_spawns_itself_and_never_a_path_substitute`
  against a hostile `omnivia-core-service` placed first on `PATH`.
- **A ready answer is now proved to be about the workspace you asked for.** The
  readiness dial used to send the descriptor's *own* claim, so a second
  workspace's real, ready service advertised in this workspace's runtime
  directory answered `ready` and managed start reported `attached`. It now sends
  the expected id and refuses a mismatched descriptor first;
  `test_a_service_answering_for_another_workspace_is_not_a_successful_start`
  fails without that guard.
- **Do not infer success from an exit code alone, and do not parse prose.** stdout
  is the versioned document and the whole of it; stderr is human. The refusal
  exit behaviour is pinned through the shipped console script by
  `test_a_refused_init_is_a_versioned_document_and_a_non_zero_exit`.

## 6. What Platform must not expect from Core v1

Stated plainly rather than left to be discovered against a green corpus:

1. **No OS code identity.** The Ed25519 release signature plus the complete file
   inventory is the v1 trust root on every supported platform. An Apple Team ID
   and designated requirement, or an Authenticode signer, is an *additional*
   product policy that v1 neither verifies nor asserts. Handoff §4.2 permits this
   ("If product policy requires…"); if the product does require it, it is a
   second, separately reviewed control.
2. **No source-controlled production signing key.** This is deliberate. The
   signing command and rotation/revocation policy are complete, while the actual
   production key is provisioned by release ownership outside the repository.
3. **No payload assembler.** The signer accepts an already-prepared platform
   runtime directory and turns it into a signed candidate. Building the native
   executable pair and other payload files remains the platform packaging job;
   signing does not silently decide how those binaries are assembled.
4. **Immutability is defence in depth, not the trust root.** The installed
   payload is made non-writable and owner-only on POSIX and read-only on Windows,
   and the same user can undo all of it. What makes tampering unusable is that
   the signature and every digest are reverified on every resolver call.
5. **No renderer-safe copy.** A refusal is one closed code plus a version, and
   deliberately carries no path, no message and no offending value — that is the
   exfiltration ceiling. Mapping those eight codes to onboarding copy is
   Platform's.

## 7. Refusal vocabulary Platform maps

`runtime_not_installed`, `runtime_metadata_invalid`, `runtime_untrusted`,
`runtime_tampered`, `runtime_layout_invalid`, `runtime_incompatible`,
`runtime_busy`, `runtime_io_failure`. Retry posture for each is tabulated in
`docs/distribution/shared-core-installation.md`.

These are distinct from `WorkspaceInitRefusal` and `ManagedStartFailure` and must
not be collapsed into them: a runtime that cannot be trusted and a folder that
cannot be adopted have different remedies and different retry postures.

## 8. Where the evidence is

| Claim | Test |
| --- | --- |
| Reference verifier agrees with every corpus case | `tests/runtime_contract/test_runtime_conformance.py` |
| A refusal leaks no path and no payload, over the whole corpus | same file, `test_a_refusal_carries_no_path_and_no_payload` |
| Canonicalisation, identity and signature vectors reproduce | same file |
| Host-level properties no fixture can carry (bounds, IO, ownership, no ambient discovery) | `packages/omnivia-core-runtime/tests/phase3/runtime/test_trusted_runtime.py` |
| Installation computes identity, hardens, publishes last, converges under concurrency | `packages/omnivia-core-runtime/tests/phase3/runtime/test_shared_runtime_distribution.py` |
| No existing destination is adopted without full verification, and a corrupt one is never overwritten or deleted | same file, `test_a_corrupt_orphan_payload_directory_is_never_adopted_or_overwritten`, `test_a_published_candidate_whose_bytes_rotted_is_refused_rather_than_reused` |
| Atomic installation never clobbers even an empty pre-existing content-derived destination | same file, `test_an_empty_directory_at_the_destination_is_refused_and_left_in_place` |
| A local publication failure never deletes a tree another installer published | same file, `test_a_publication_failure_never_deletes_a_tree_another_installer_published` |
| A payload for the other architecture is a compatibility refusal | `contracts/runtime/v1/fixtures/invalid/wrong-payload-architecture.json`, and `test_a_payload_built_for_the_other_architecture_is_incompatible` |
| The reference verifier is never looser than the published `keyId` / `relativePath` policies | `tests/runtime_contract/test_runtime_conformance.py`, the two `no_..._the_schema_rejects_is_accepted` tests |
| `--init` / `--managed-start` through the shipped console script | `packages/omnivia-core-runtime/tests/phase3/runtime/test_managed_start.py` |
| The packaged wheel really carries the corpus, read through `importlib.resources` | `scripts/check-package-builds.sh` |
| Trust and path-safety on macOS, Linux and Windows | `.github/workflows/phase2-platform.yml`, step "Run trusted-runtime trust and path-safety tests" |

Cases that need a capability the host lacks — POSIX modes on Windows, symlinks
where they cannot be created — skip with a stated reason rather than reporting a
verdict they did not observe. The Phase 2 rows run with `-rs` so those skips are
visible per platform instead of disappearing into a green row.
