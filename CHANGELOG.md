# Changelog

<!--
Maintenance rule: add one entry per accepted merge into `main`, in the same
change that would publish the successor Core baseline pointer in `omnivia-pm`
(`docs/repo-governance/omnivia-core-current-baseline-pointer.json`). One line
per lane, not per commit; take the scope wording from that lane's pointer entry
so the two records cannot drift. Pointer 005 went seven merges stale by
publishing per closeout rather than per merge — do not repeat that here.
Nothing enforces this rule mechanically today.
-->

All notable changes to OmniVia Core are recorded here.

This file follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) with
one deliberate departure: there is only one version-level section, `Unreleased`,
because **Core has never released**. There is no git tag, no GitHub release, and
all five first-class distributions carry `version = "0.1.0"`. "v0.6" is a
product milestone (V06-1 … V06-8), not a package version, so it appears here as
a development record inside `Unreleased` rather than as a released version
heading. Entries are grouped by accepted lane and listed newest first, because
the unit of acceptance in this repository is a reviewed merge, not a release.

The accepted-baseline authority is `omnivia-pm`:
`docs/repo-governance/omnivia-core-current-baseline-pointer.json`. Where this
file and that pointer disagree, the pointer wins.

---

## Unreleased

Current accepted baseline: `27a958fd3733c9d4e0d9b4831167a8c938b8cd62`
(pointer `OMNIVIA-CORE-CURRENT-BASELINE-POINTER-009`, 2026-08-05).

### v0.6 status — what is not true yet

A reader of release notes can reasonably infer capabilities from a merge list.
These inferences would all be wrong. The first three are recorded verbatim as
not authorized in the baseline pointer:

- **Core does not serve HTTP.** The adapter is declared embedder-only for v0.6
  and has no production caller.
- **V06-4 is not complete.** Core has no TLS capability at all —
  `service/http_transport.py` never imports `ssl` and `https` cannot be
  expressed — so the non-loopback half is build-then-prove, not prove-only.
- **`get_workspace` does not exist.** It has zero occurrences tree-wide; the
  catalogue entry carrying the decided shape is `workspace.inspect`.

A fourth, checkable against this tree rather than against the pointer:

- **No peer identity is verified anywhere.** `SO_PEERCRED`, `getpeereid` and
  `peercred` have zero occurrences tree-wide. Nothing in this window added
  authenticated peer identity to any transport.

Completion stage as recorded by the pointer: V06-1 complete; V06-4 P1b,
P2b-local and HTTP-loopback integrated, with HTTP unserved by default and
non-loopback TLS outstanding; V06-2, V06-3 and V06-5 through V06-8 outstanding.

### v0.6 owner declarations

Both declarations are recorded in
[`docs/development/omnivia-core-staged-startup-and-embedder-only-http-2026-08-05.md`](docs/development/omnivia-core-staged-startup-and-embedder-only-http-2026-08-05.md).
That record instructs any later release record to carry the same declaration and
cite it rather than paraphrase it, so this section cites and quotes; it does not
restate.

**HTTP is embedder-only for v0.6.** The record's normative declaration, section
2.2, verbatim:

> OmniVia Core v0.6 contains a loopback-safe HTTP adapter that may be wired by
> an approved embedder or conformance harness. The standard Core service and CLI
> do not expose an HTTP listener because no approved credential source or
> bearer-session resolver has yet been defined. This does not remove
> authenticated HTTP from the target architecture.

**`coordinated_startup` is staged, not orphaned.** Section 1 of the same record
names its four intended consumers in order, its boundary rules, and its removal
trigger at the V06-3 closeout. It implements the `PM ADR-037` startup sequence.
Read section 1 for the normative text; it is not reproduced here.

### v0.6 development — accepted lanes

Scope wording is taken from the baseline pointer's `acceptedIntegrations`
entries, except for the first entry as noted above. An entry records an
integrated candidate, not a completed lane; lanes that the pointer marks
incomplete say so.

- **`published_at` value domain and narrow endpoint validator** — the descriptor
  decoder refuses a `published_at` that is not a Timestamp, and the probe refusal
  answers for its own field. Independently reviewed with no coverage loss,
  measured over 457,000+ cases. Recorded in pointer 009.
  ([#38](https://github.com/claytonread/omnivia-core/pull/38), `27a958f`)
  Residual: this closes one property of a wider gap. The pointer records
  `VALUE-DOMAIN-SWEEP` as unowned — 56 patterned string definitions emit a
  pattern constant that no generated decoder applies, and `published_at` was one
  of 33 Timestamp-typed properties. The census in this pull request is that
  blocker's scope statement.
- **Collection and lint configuration** — a bare `pytest` run reaches the whole
  checkout (13961 collected, previously 9358 with zero from `packages/`),
  guarded by a containment check that cannot rot on a count. Ruff's resolution
  semantics are pinned by `required-version` with the dependency bound placed
  where CI actually installs from, the coupling derived by test rather than
  asserted by comment, and B904 measured to endorse the construct the
  raise-discipline guard forbids, so it is explicitly ignored.
  ([#37](https://github.com/claytonread/omnivia-core/pull/37), `38276c3`)
- **Owner declarations for staged startup and embedder-only HTTP** —
  `coordinated_startup` declared staged with four named consumers, boundary rules
  and a removal trigger at the V06-3 closeout; the HTTP adapter declared
  embedder-only and intentionally unreachable from the standard service for
  v0.6. Two pieces of existing prose that contradicted the declarations were
  corrected. No behaviour changed.
  ([#36](https://github.com/claytonread/omnivia-core/pull/36), `96102be`)
- **M2 storage residuals** — published descriptors take ownership and carry a
  protected DACL on Windows, applied before the rename. The created-ancestor
  scan is replaced by createdness decided at the creating syscall, closing a
  leftover that was permanent rather than transient.
  ([#35](https://github.com/claytonread/omnivia-core/pull/35), `2659ce8`)
  Residual: the descriptor-publication mutation matrix contains no owner
  mutation, so a green `windows-latest` row is not evidence that `/setowner`
  runs.
- **Response version facts** — the service answers with its own supported
  window, negotiated selection and computed status instead of echoing the
  caller's claim.
  ([#34](https://github.com/claytonread/omnivia-core/pull/34), `4816b65`)
- **V06-4 P2b HTTP v1 loopback** — loopback HTTP adapter, refused by default.
  ([#33](https://github.com/claytonread/omnivia-core/pull/33), `2fd5155`)
  Lane incomplete. Present but **not served**: `--http-endpoint` defaults to
  `None` and the console script supplies no credential resolver, so any HTTP
  bind exits 2. It is a seam an embedder can wire, not a boundary this service
  serves. The non-loopback TLS half remains blocked on infrastructure.
- **Raise-site repairs** — nine suppressed exceptions no longer reachable
  through `__context__`; the accepted-site ratchet is empty and passing.
  ([#32](https://github.com/claytonread/omnivia-core/pull/32), `e7870ab`)
- **Ordinal-vs-contract-version trap** — a malformed startup version now raises
  naming the field instead of a fail-closed refusal quoting a window the caller
  matched. ([#31](https://github.com/claytonread/omnivia-core/pull/31),
  `e5b34bf`)
- **CLI API version** — the CLI claims the contract version instead of a stale
  literal the one live comparison refused.
  ([#29](https://github.com/claytonread/omnivia-core/pull/29), `c376b5f`)
- **Repo hardening guards** — import/install alignment and raise-discipline
  guards, both merge-blocking today.
  ([#30](https://github.com/claytonread/omnivia-core/pull/30), `f760f63`)
- **P1b corpus gaps m3–m8** — closed the recorded P1b corpus residuals with no
  source change; the behaviour was present and untested.
  ([#28](https://github.com/claytonread/omnivia-core/pull/28), `af8844c`)
- **Boot identifier** — `_boot_id()` validates every platform candidate at one
  exit against the public grammar; Darwin prefers `kern.bootsessionuuid` over
  the clock-derived `kern.boottime`.
  ([#27](https://github.com/claytonread/omnivia-core/pull/27), `efb89ad`)
  Residual: change-across-boots for the real macOS UUID is proved by
  construction and kernel semantics, not by an observed reboot.
- **M2 descriptor publication** — the runtime publishes the contract
  `ServiceEndpointDescriptor` at `0600` in `0700` directories; bootstrap and the
  CLI reader migrated to the contract shape.
  ([#26](https://github.com/claytonread/omnivia-core/pull/26), `acaeeac`)
  Residual: end-to-end closure is Linux/CI-only — the macOS boot-id defect kept
  `service.discover` unanswerable there, so integrated discovery still could not
  work on macOS at this merge. Addressed by the boot identifier lane above.
- **V06-4 P1b** — client descriptor provenance, discovery and compatibility
  negotiation over a trusted installation root.
  ([#24](https://github.com/claytonread/omnivia-core/pull/24), `acb4ae5`)
  Lane incomplete at this merge: the P2b HTTP v1 trust boundary was absent and
  P1b corpus gaps m3–m8 were open. Both are addressed by later entries above;
  V06-4 as a whole remains incomplete.
- **R2 residual delivery** — phase2-platform commentary correction, current
  npm/Python dependency truth record, Apps/Pro boundary rejection across all
  five first-class public Core distributions.
  ([#23](https://github.com/claytonread/omnivia-core/pull/23), `8cc6bd3`)
- **V06-4 P2b (partial)** — OVC1 canonical local framing over Unix sockets and
  Windows named pipes; transitional newline and Python-private framing removed
  from the production path.
  ([#22](https://github.com/claytonread/omnivia-core/pull/22), `e75fa34`)
  Lane incomplete: the HTTP v1 trust boundary was absent, and a bounded
  successor packet is required before V06-4 P2b is complete.

Accepted migration lineage head: `0011_projection_lifecycle.sql`. The successor
gate is closed — no migration successor may start before a single owner is
named for the next migration number.

### Foundation before the current pointer window

The baseline pointer records milestones M1–M5 as accepted durable foundation but
does not carry per-lane scope summaries for the merges that built them. The
entries below are therefore titled from their merge commits rather than from a
governance record, and are listed at lower resolution for that reason. Newest
first.

- G1 typed PDF page access
  ([#21](https://github.com/claytonread/omnivia-core/pull/21), `fa4560c`)
- App Shell preflight quality gates cleared
  ([#20](https://github.com/claytonread/omnivia-core/pull/20), `e4c53fc`)
- Stable Ruff lint baseline pinned
  ([#19](https://github.com/claytonread/omnivia-core/pull/19), `7e01709`)
- Governed Host Contract v1
  ([#18](https://github.com/claytonread/omnivia-core/pull/18), `5135558`)
- V06-1 M5 projection lifecycle, and its failed-run chronology repair
  ([#16](https://github.com/claytonread/omnivia-core/pull/16), `b9a77a6`;
  [#17](https://github.com/claytonread/omnivia-core/pull/17), `4b64628`)
- V06-1 M4 durable job history
  ([#15](https://github.com/claytonread/omnivia-core/pull/15), `0278d3c`)
- V06-1 M3 governed truth and relations storage
  ([#13](https://github.com/claytonread/omnivia-core/pull/13), `9fc3af4`)
- V06-1 M2 blobs, staged sources and evidence schema, and the successor-tolerant
  M1 acceptance it needed
  ([#11](https://github.com/claytonread/omnivia-core/pull/11), `a48baeb`;
  [#12](https://github.com/claytonread/omnivia-core/pull/12), `113d3d6`)
- V06-1 M1 durable audit and idempotency migration, on a pristine migration
  baseline ([#9](https://github.com/claytonread/omnivia-core/pull/9),
  `5973279`; [#10](https://github.com/claytonread/omnivia-core/pull/10),
  `75db9b4`)
- V0.6 C0b architecture gate traceability ledger
  ([#8](https://github.com/claytonread/omnivia-core/pull/8), `b58f91a`)
- V0.6 G0 foundation
  ([#7](https://github.com/claytonread/omnivia-core/pull/7), `2c6a4c8`)
- Public service endpoint descriptor contract (Phase 3 P0)
  ([#6](https://github.com/claytonread/omnivia-core/pull/6), `ba07266`)
- Workflows upgraded to Node 24-native actions
  ([#5](https://github.com/claytonread/omnivia-core/pull/5), `87389ef`)
- Repaired Stream B Phase 2 integration
  ([#4](https://github.com/claytonread/omnivia-core/pull/4), `95745bf`)
- Core acceptance workflow bootstrap
  ([#2](https://github.com/claytonread/omnivia-core/pull/2), `15d1db6`)

---

## Maintaining this file

Add an entry when a lane is merged into `main` — the same trigger that publishes
the successor baseline pointer in `omnivia-pm`. Take the scope wording from that
lane's pointer entry; if the pointer records `completesLane: false` or an
`openResidual`, carry that here too, since a merge list read without it
overstates what Core can do.

Nothing enforces the changelog half of this rule: there is no changelog guard in
`.github/workflows/`, so an entry can be forgotten without any check failing.
The pointer half is enforced — `omnivia-pm`'s `.github/workflows/pointer-cadence.yml`
runs `scripts/check-pointer-cadence.mjs` on pull requests, and its first run
failed on exactly this gap before pointer 008 was published.

Repository qualification for citations: architecture decision records cited from
Core are qualified by repository, so `PM ADR-037` means `omnivia-pm`:
`docs/adr/ADR-037-core-service-ownership-bootstrap-and-workspace-fencing.md`.
