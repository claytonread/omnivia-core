# OmniVia Core trusted runtime and Workspace bootstrap — Platform handoff

Date: 2026-09-10

Status: Core contract implemented; Platform integration implemented and verified;
production release proof remains open

Core repository: `/Users/claytonread/Projects/omnivia-core`

Core baseline inspected: `5797d52b8bb6f916789400d94995d8720605494f`

Platform consumer checkout: `/Users/claytonread/Projects/worktree-omnivia-platform-assistant-ui-wrapper-spike`

Platform consumer baseline: `656ae59e0352150c1a443195c4dc973d3f660d87`

## Implementation status (2026-09-11)

The contract-first implementation described by this handoff is complete in
source and focused integration evidence:

- Core PR #104 landed the trusted-runtime contract, verifier, conformance
  corpus, Workspace init/adopt qualification and managed-start behavior at
  `190a7742393aa7b259630de89871566b188bc2f5`.
- Platform consumes the signed cross-language corpus, verifies the selected
  immutable payload before either launch, invokes the exact verified service
  path for `--init` and `--managed-start`, persists the resulting Workspace
  binding, restores it after restart and maps only stable failure codes to the
  renderer.
- Core MCP evidence is reconciled with the current 27-operation catalogue at
  integration commit `a94efd6`: exactly six reviewed read operations are
  exposed and the other 21 operations have explicit omission reasons. The
  accepted architecture evidence is intentionally limited to gates g07, g08,
  g17, g20 and g29. Gate g16 remains pending because the candidate proof uses
  simulated Claude-format host rows rather than launching a real Claude client;
  g22 retains partial MCP, CLI and managed-start evidence but remains pending.
- That six-tool result completes the approved MCP retrieval profile, not the
  intended bidirectional standalone product. An MCP-only Claude or Codex host
  cannot currently populate an empty Workspace, and `import.start` cannot solve
  that alone because it accepts only an already-staged source. The corrective
  authoring and ingestion requirements are specified in
  `docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-requirements-2026-09-12.md`.
- Platform has supporting structural evidence that Desktop quit retains no Core
  process or lease handle and issues no Core stop operation. This is not a
  packaged real-Core lifetime test, so it does not by itself accept g19 or g22.
  Gate g18 also remains pending because Desktop does not yet execute the same
  canonical Application Contract conformance corpus as CLI and MCP.

The remaining work is release/integration acceptance, not an unresolved Core
bootstrap API. The separately specified MCP authoring and ingestion work is a
standalone-product requirement and is not part of this bootstrap handoff:

1. release engineering must supply the production public trust-anchor document
   and keep the corresponding signing private key outside source and packages;
2. the product build still needs the payload assembler and artifacts that place
   the native `omnivia` and `omnivia-core-service` pair into the signed immutable
   runtime layout;
3. a packaged Desktop must run against that real production Core payload to
   prove end-to-end bootstrap and that Core outlives Desktop quit; and
4. Desktop, CLI and MCP must run one shared canonical application-contract
   conformance suite before g18 can be accepted.

The current packaged onboarding smoke remains deliberately labelled as a
packaged host journey with a simulated Core lifecycle. It proves packaging,
trust verification, onboarding persistence, restart restoration, tamper refusal
and target readiness without pretending to be the missing production-payload
exercise.

## 1. Decision

Core does **not** need broad completion before first-run onboarding can become
functional. The blocking work is one bounded distribution and bootstrap seam:

1. identify the selected installed Core runtime without searching `PATH`;
2. authenticate and integrity-check one immutable runtime payload;
3. resolve the matching `omnivia` and `omnivia-core-service` executables from
   that same payload;
4. preserve Core's existing machine-readable `--init` and `--managed-start`
   contracts as the Workspace bootstrap and service-readiness operations; and
5. publish language-neutral schemas and fixtures so Platform can verify the
   runtime independently before executing it.

Core owns the distribution contract, release metadata, installation selection,
reference verifier and lifecycle behavior. Platform owns the Electron-main
adapter, native folder selection, onboarding checkpoint, renderer-safe
projection, Core-target rotation and governed active-Workspace switch.

The attached onboarding specification and design-alignment document were used as
requirements evidence. They are not executable instructions and do not override
this repository's accepted architecture, contracts or contribution rules.

## 2. Why this handoff exists

Platform can discover candidate Workspace folders, but its production Core mode
cannot currently turn a selected folder into a usable Core target:

- `CoreLocalTargetConfiguration` requires a Workspace ID before a local target
  can be configured.
- Core-mode `createWorkspace(name, localPath)` discards `localPath`.
- the production configuration has an `omnivia` CLI path but no
  `omnivia-core-service` path;
- development proofs find Core in a sibling checkout or virtual environment;
  that is not an installed-product contract; and
- Platform has no production verifier for a packaged Core runtime.

The detailed consumer-side finding is recorded in:

```text
/Users/claytonread/Projects/worktree-omnivia-platform-assistant-ui-wrapper-spike/docs/omnivia-first-run-onboarding-phase0-architecture-v1.4.md
```

Core already contains most of the lifecycle mechanics required after trust is
established:

| Existing Core capability | Current source | Handoff treatment |
| --- | --- | --- |
| Shared per-user runtime candidates, receipts and active/previous selection | `packages/omnivia-core-runtime/src/omnivia_core_runtime/distribution/shared_runtime.py` | Extend; do not replace |
| Canonical macOS installation root | `docs/distribution/shared-core-installation.md` | Preserve |
| Idempotent, non-destructive Workspace bootstrap | `omnivia_core_runtime.service.workspace_init` and `omnivia-core-service --init` | Reuse unchanged unless a contract defect is found |
| Compatible existing Workspace reuse | `initialise_workspace()` | This is the Core-side adopt path; do not add a second mutation |
| Managed attach/start with live readiness | `omnivia_core_runtime.service.managed_start` and `--managed-start` | Reuse |
| Portable Workspace identity and compatibility | `omnivia_core.workspace.manifest` and `.compatibility` | Preserve as authority |

The missing property is not process spawning by itself. It is proving, before
the first process launch, that the two executable paths came from one approved,
untampered Core release.

## 3. Required outcome

After this Core task and its Platform follow-up land, Electron main must be able
to perform the following sequence without a sibling source checkout, Python
virtual environment, ambient `PATH` lookup or renderer-supplied path:

```text
resolve selected installed runtime
  -> verify release authenticity and complete payload integrity
  -> derive exact paired executable paths
  -> revalidate the selected Workspace folder token in Platform main
  -> invoke exact omnivia-core-service path with --init
  -> parse the versioned Workspace-init result and obtain workspace_id
  -> invoke that same exact service path with --managed-start
  -> require a live ready answer for the same workspace_id
  -> configure Platform's Core target with the paired omnivia path
  -> perform Platform's existing governed Workspace switch
  -> persist completion only after the switch succeeds
```

Creating and adopting converge on the existing `--init` behavior:

- an eligible empty directory is initialized and receives a minted identity;
- an existing compatible Workspace is revalidated and retained with its
  existing identity; and
- incompatible, busy, unrelated or unqualified locations are refused without
  being silently converted.

Core does not choose which folder to use and does not mark a Workspace active in
the desktop shell.

## 4. Core-owned deliverables

### 4.1 Versioned runtime payload manifest

Add a public, language-neutral manifest for an installed Core runtime payload.
The exact filenames may follow repository convention, but the accepted
contract must define all of these facts:

- contract/schema version;
- exact Core release version;
- supported operating system and architecture;
- payload identity;
- fixed relative path and content digest for `omnivia`;
- fixed relative path and content digest for `omnivia-core-service`;
- a deterministic inventory of every security-relevant payload file, including
  relative path, digest and executable-mode expectation;
- the minimum/maximum protocol or compatibility bounds needed by a consumer;
- release signing key identifier and signature algorithm; and
- a detached signature over a specified canonical byte representation of the
  unsigned manifest.

The payload identity must be computed by one documented algorithm. It must not
trust a caller-provided digest, and it must avoid a self-referential hash. A
recommended simple construction is a SHA-256 digest over the canonical unsigned
manifest containing the sorted file inventory and each file's SHA-256 digest.

Only fixed product executable names are admitted. The manifest is not a generic
command launcher and cannot add arguments, environment variables, working
directories or arbitrary executable paths.

### 4.2 Authentic active-runtime selection

Keep the current deterministic `active.json` / `previous-known-good.json`
selection model, but treat the active record only as an untrusted selection
hint until its candidate has been verified.

The active record must identify one candidate by release version and payload
identity. Resolution must derive the candidate directory under the canonical
installation root; it must never accept an absolute payload path from the
record.

The selected candidate is trusted only after the release-signed payload
manifest, directory identity and full file inventory all verify. This avoids
requiring an on-device release-signing private key merely to update
`active.json`.

If product policy requires OS code identity in addition to the release
signature, define and test that requirement per supported platform. On macOS,
verification must cover the expected Team ID/designated requirement for the
shipped artifacts. A valid local digest alone proves integrity against a signed
manifest; it does not prove who issued the manifest.

### 4.3 Fail-closed resolver

Add a Core-owned resolver/reference verifier that accepts an explicit absolute
installation root and approved release trust anchors, then returns either a
verified runtime descriptor or a closed refusal.

The successful main-process descriptor should contain only the fields needed to
bind the runtime:

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

This is an illustrative field shape, not permission to freeze those exact names
without the normal contract review. The accepted schema must be consumable from
TypeScript without importing Python implementation code.

The resolver must:

- reject absent, malformed, oversized or unsupported-version records;
- reject absolute or escaping relative paths in installation metadata;
- reject symlinks at the installation selector, candidate, manifest and
  executable boundaries;
- reject duplicate, missing and extra security-relevant inventory entries;
- verify every declared digest using bounded reads;
- require both executables to be regular files in the same candidate payload;
- enforce owner/mode and platform code-signing policy;
- verify the release signature against an approved key, including key rotation
  rules;
- return no usable paths on any failure; and
- reverify on each process launch. A previously saved descriptor is a cache hint,
  never continuing authority.

No recursive runtime search, shell, `which`, ambient `PATH`, network install or
fallback to a sibling checkout is allowed in the production resolver.

### 4.4 Installation and immutability enforcement

Strengthen `SharedRuntimeInstallation.install_candidate()` so an installed
candidate is checked against the manifest rather than accepting the supplied
`payload_digest` as truth.

Installation must:

1. verify the staged payload completely;
2. refuse all symlinks and path escapes;
3. place it only at `runtimes/<semver>/<payload-identity>/`;
4. make the installed payload non-writable under the supported platform policy;
5. publish the candidate index atomically only after verification and hardening;
6. never overwrite a conflicting candidate; and
7. retain the existing consumer-safe active/previous-known-good behavior.

If practical filesystem permissions cannot provide immutability against the
current user, the contract must say so plainly. Launch-time signature and digest
reverification remains mandatory and is the security control; a read-only mode
is defense in depth, not the trust root.

### 4.5 Cross-language schemas and conformance fixtures

Publish schemas plus canonical valid/invalid fixtures covering at minimum:

- valid runtime payload and active selection;
- wrong release signature;
- unknown signing key;
- executable digest mismatch;
- two executables from different payloads;
- active-record path traversal;
- symlinked candidate, manifest or executable;
- missing/extra inventory member;
- unsupported schema and incompatible Core release;
- incorrect executable mode/ownership;
- payload directory name inconsistent with payload identity; and
- valid key rotation from an accepted old key to an accepted new key.

Provide language-neutral canonicalization and signature test vectors. Platform
must be able to prove its TypeScript verifier produces the same accept/refuse
decisions as Core's reference verifier.

### 4.6 Existing lifecycle contract qualification

Do not create a new broad bootstrap service if the existing two modes satisfy
the contract. Qualify and pin these exact calls as the consumer seam:

```text
<verified service_path>
  --init
  --workspace <absolute main-owned selected folder>
  --installation-state <absolute Platform-owned installation state>
  --core-version <verified release version>
```

and then:

```text
<same verified service_path>
  --managed-start
  --workspace <same canonical folder>
  --installation-state <same installation state>
  --endpoint <Platform-main-generated local endpoint>
  --expected-manifest-digest <sha256 over the selected workspace.json bytes>
  [--required-absent-manifest <preferred registered workspace.json>]
  --core-version <same verified release version>
```

Invoking the exact verified `omnivia-core-service` path matters. Its existing
managed-start implementation then selects `sys.argv[0]` before considering
`PATH`, so the child service remains the same verified executable. Add an
acceptance test that pins this property; do not rely only on the current
implementation comment.

The `--init` stdout document remains the authority for the minted or retained
`workspace_id`. The `--managed-start` stdout document remains the authority for
attached/started status and live readiness. Human diagnostics remain on stderr.
The digest and optional absence guard freeze the caller's final workspace
selection through both the launcher and the service process; older bootstrap
consumers may omit them, but a consumer that independently selects a workspace
should carry both parts of that selection rather than authorize by path twice.

If implementation finds either output contract insufficient, widen it only by
the repository's versioning rules, with fixtures and a published compatibility
mapping. Do not parse prose or infer success from exit code alone.

## 5. Security invariants

These are acceptance requirements, not implementation suggestions:

1. **Trust before execution.** No version, status, init or readiness probe may
   execute until the runtime payload is verified.
2. **One payload, one pair.** `omnivia` and `omnivia-core-service` must resolve
   from the same authenticated immutable candidate.
3. **No command authority in metadata.** Metadata cannot supply argv, shell
   fragments, environment variables or a working directory.
4. **No ambient discovery in production.** Runtime resolution cannot use
   `PATH`, `which`, recursive filesystem search or a source checkout.
5. **Canonical containment.** Every resolved path remains beneath the approved
   candidate root after normalization and no path component is a symlink.
6. **Revalidate at use.** Persisted runtime roots and active records are hints;
   each launch rechecks selection, signature, inventory and executable identity.
7. **Workspace identity comes from Core.** Platform cannot mint or replace a
   Workspace ID during create/adopt.
8. **Adoption is non-destructive.** An existing compatible manifest and database
   are retained; mismatch, unrelated content, unsupported format and busy state
   fail closed.
9. **No renderer authority.** Raw runtime paths, Workspace paths, endpoint URIs,
   child output, PIDs and signing details never cross to the onboarding renderer.
10. **Activation stays governed.** Successful init/start does not itself make a
    Workspace active in Platform.
11. **Bounded subprocesses.** Main enforces explicit time, output and process
    cleanup bounds around both calls.
12. **Same-operation retry is safe.** A crash after Core writes but before
    Platform checkpoints must converge on the same Workspace identity when the
    same folder is retried.

## 6. Failure contract

Core-owned runtime resolution needs fixed, payload-free machine codes. Exact
names require normal review, but the taxonomy must distinguish:

| Class | Meaning | Retry posture |
| --- | --- | --- |
| `runtime_not_installed` | No active installed candidate exists | Retry only after installation/repair |
| `runtime_metadata_invalid` | Selection or manifest is malformed/unsupported | Repair required |
| `runtime_untrusted` | Signature/key/code identity is not approved | Never execute; repair required |
| `runtime_tampered` | Payload identity or file digest does not match | Never execute; repair required |
| `runtime_layout_invalid` | Pair missing, split, escaping, symlinked or wrong mode | Never execute; repair required |
| `runtime_incompatible` | Authentic release cannot serve the consumer contract | Install compatible Core |
| `runtime_busy` | Installation selection is being atomically changed | Bounded retry |
| `runtime_io_failure` | Bounded local read failed | Bounded retry or repair |

These resolution failures are distinct from the already-versioned
`WorkspaceInitRefusal` and `ManagedStartFailure` vocabularies. Do not collapse
Core init/start results into a single generic bootstrap error.

Machine responses may contain main-process diagnostics, but renderer-safe copy is
Platform's responsibility. Core output must remain bounded and must not contain
credentials or file contents.

## 7. Core acceptance scenarios

### 7.1 Distribution and trust

- A correctly signed packaged runtime resolves the paired executable paths.
- A single-byte change to either executable is refused before any process runs.
- A valid payload copied under the wrong digest directory is refused.
- Replacing `active.json` with an escaping path cannot escape the installation
  root.
- A symlink at every checked boundary is refused on macOS/Linux and by the
  platform-equivalent Windows control.
- An attacker-controlled executable earlier on `PATH` is never invoked.
- A candidate with a valid integrity digest but an unapproved issuer is refused.
- Supported signing-key rotation accepts both keys only for the documented
  transition window and rejects retired keys afterward.
- Concurrent installation/reconciliation never exposes a half-written candidate
  as active.
- Previous-known-good and other consumers' receipts retain their current safety
  properties.

### 7.2 Workspace init/adopt

- Init of an eligible empty local folder returns `initialised` and a valid
  Workspace ID.
- Repeating init returns the same Workspace ID and does not duplicate or replace
  state.
- Init of an existing compatible Workspace returns `already_initialised` with
  the retained ID.
- An incompatible/malformed manifest, identity mismatch, unrelated non-empty
  directory, unqualified filesystem, foreign installation state and busy
  Workspace each fail closed with no unauthorized mutation.
- A crash/retry after database bootstrap or manifest publication converges under
  the existing idempotency guarantees.
- The machine-readable stdout schema and exit behavior are pinned through the
  actual console entry point, not only in-process Python calls.

### 7.3 Attach/start

- The same verified service executable performs managed start and spawns itself,
  not a `PATH` substitute.
- Concurrent starters converge on one authoritative service.
- A published descriptor without a live readiness answer is not success.
- A service answering for another Workspace ID is rejected.
- Timeout/spawn/readiness failures clean up only the child and transient
  descriptor owned by that attempt.
- Successful output carries the same Workspace ID returned by init/adopt.

### 7.4 Cross-language conformance

- Python reference verification and Platform TypeScript verification agree on
  every canonical fixture.
- Canonical bytes and signatures are reproducible on macOS, Linux and Windows.
- Unknown additive fields follow the explicit schema/version policy rather than
  being silently trusted.

## 8. Platform integration contract after Core lands

The follow-up Platform change should be limited to Electron main and existing
runtime boundaries:

1. Implement the Core-defined verifier independently in TypeScript using the
   published fixtures and approved release keys/code requirements.
2. Add the verified `servicePath` and runtime identity to the main-only target
   configuration; keep them out of renderer snapshots.
3. Resolve the Core installation only from approved production sources:
   canonical managed installation metadata, a signed runtime bundled with the
   signed application where supported, or the existing explicit development
   override under development policy.
4. Resolve and revalidate the Workspace folder through Platform's existing
   opaque onboarding token mechanism.
5. Invoke `--init`, parse the versioned result and require the returned
   Workspace ID.
6. Invoke `--managed-start` on the same service path, then require live ready
   state and matching Workspace ID.
7. Configure the existing `CoreClientAdapter` with the paired verified
   `omnivia` path, installation state and Workspace ID.
8. Use the existing `establishCoreWorkspace()` / `applyWorkspaceSwitch()` path
   as the only active-Workspace writer.
9. Re-read authoritative state and persist the onboarding checkpoint/completion
   only after the governed switch succeeds.

Platform must map Core failures into stable safe onboarding issues. It must not
expose Core's raw path-bearing reasons or `child_output` to the renderer.

## 9. Explicit non-goals

This task does not:

- complete all of Core;
- add Workspace folder discovery to Core;
- add Electron, renderer or onboarding state to Core;
- change which Workspace Platform considers active;
- make a renderer callback or folder path authoritative;
- introduce network installation or automatic download;
- replace the existing portable Workspace manifest;
- merge Workspace init and application-level `workspace.create` into one
  ambiguous operation;
- create a long-running daemon, login item or background auto-start policy;
- add a generic signed-command framework; or
- redesign Core service transport, fencing, readiness or shutdown.

## 10. Implementation order

1. Freeze the threat model, canonical payload-digest construction, release-key
   lifecycle and per-platform code-identity rule.
2. Add versioned schemas, canonicalization/signature vectors and invalid
   fixtures.
3. Implement and test the reference payload verifier/resolver.
4. Integrate verification into candidate installation and active selection.
5. Pin the exact-path `--init` and self-spawning `--managed-start` console
   journeys using packaged artifacts.
6. Build/package a real candidate and run cross-platform qualification.
7. Publish the Core contract checkpoint for Platform consumption.
8. Implement the independent Platform TypeScript verifier and onboarding
   adapter in the Platform repository.
9. Run an end-to-end packaged Electron proof for create, adopt, restart-resume,
   attach/start and governed activation.

Steps 1–7 are Core-owned. Steps 8–9 are a separate Platform implementation task
and must not be smuggled into this repository.

## 11. Definition of done for the Core handback

The Core lane is ready to hand back only when it provides:

- reviewed runtime-manifest and active-selection schemas;
- an explicit signature trust/key-rotation policy;
- a reference resolver that returns the exact paired executable paths only after
  complete verification;
- installation enforcement that computes and verifies payload identity;
- canonical valid/invalid fixtures and signature vectors usable by TypeScript;
- qualified packaged entry-point tests for `--init` and `--managed-start`;
- platform-specific trust and path-safety tests on all supported operating
  systems;
- documentation of any version or refusal-vocabulary change;
- a clean `./scripts/preflight` result on the task branch;
- a pull request with the required `Core acceptance` and Phase 2 Platform checks
  green at its latest commit; and
- a handback note naming the exact contract files, generated artifacts, fixture
  directories, release public keys/key IDs and minimum compatible Core version
  Platform must consume.

The end-to-end product is complete only after the separate Platform lane verifies
the same fixtures, invokes the packaged runtime by exact path and proves the
governed Workspace activation journey. A green Core handback alone does not make
the Electron onboarding UI functional.

## 12. Open decisions that must be closed before coding

Only four decisions remain architecture-level:

1. the canonical payload identity construction and whether it covers every file
   or a closed security-relevant inventory plus signed package/container identity;
2. the release signature algorithm, key storage, rotation and revocation policy;
3. the exact fixed executable paths for each supported package layout; and
4. the supported-platform ownership, mode and OS code-signing requirements.

These decisions belong to Core distribution/release ownership. They must be
resolved before implementation because changing them later changes the trust
contract Platform independently enforces.
