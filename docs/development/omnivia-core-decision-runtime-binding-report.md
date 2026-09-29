# Decision Runtime — Phase 0 binding report and implementation plan

Specification: `SPEC-CORE-DEC-001` v1.0 (22 September 2026)
Binding: `omnivia-core` main, post PR #117 (`8fb284f0b71fe859e67094c839d9768077f29ae8` and descendants)
Companion plan: `decision-runtime-implementation-plan-v1.0.md` (PR slicing, dependency graph, AT-test mapping)

## 1. Verified repository binding

Every path below was inspected in the current checkout on the binding date.

| Spec seam | Bound location | Evidence |
|---|---|---|
| Frozen operation catalogue | `contracts/application/v1/schemas/operations.schema.json`, `x-omnivia-operation-catalogue` (28 v1 operations) | File description states the Python and TypeScript catalogues are generated from it; no second catalogue exists |
| Catalogue registry | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/operations.py` — `OPERATION_CATALOGUE` import, `APPLICATION_OPERATIONS` frozenset, separate `SERVICE_OPERATIONS` probes | A registry-only addition is structurally impossible; the schema amendment is the only entry route (AT-05) |
| Contract generation | `scripts/generate-application-contracts.py` → `src/omnivia_core/contracts/v1/generated.py` + `generated/typescript/application/v1/index.ts`; `baseline/inventories/public-exports.json` inventory | Regeneration required for any catalogue change |
| Service composition | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/main.py`; authorisation path already has `admission.py`, `authorization.py`, `capability_gateway.py` | [C1] confirmed |
| Handler pattern | `service/handlers/` | Decision handlers follow the existing handler conventions |
| Workspace migrations | `storage/migrations.py` + `storage/migration_files/` | Latest `0042_runtime_stop_progress.sql`; Decision migrations begin at **0043** |
| Installation-owned state | `storage/installation_migrations.py` + `storage/installation_store.py` | Model installation state belongs here (§13); numbering is separate from workspace migrations |
| Durable jobs | `storage/jobs.py` + `service/jobs.py` | Evaluation jobs reuse this; no competing job API |
| Outbox/events | `storage/semantic_events.py` transactional pattern | `decision.*` events follow it (§14.5) |
| Candidate/governance path | `storage/governance.py`, `storage/governed.py` | Derived classifications enter as candidates (§14.3, [D3]) |
| Public contracts | `src/omnivia_core/contracts/v1/` (`generated.py`, `semantics_*.py`) | Decision DTOs/errors as a new `semantics_decisions.py` module; no model imports (D-02) |
| Thin clients | `packages/omnivia-core-cli`, `packages/omnivia-core-client`, `packages/omnivia-core-mcp` (`server.py`, `manifest.py`, `generated_schema_projection.py`) | Transport only (D-02) |
| Settings | `apps/core-status-menu-macos/` — `WebSettingsWindowController`, `SettingsBridge`, shared bundle under `Resources/prototype/` | Processing-pane extension via the existing allowlisted bridge (C3, C4, D-10) |

## 2. Required binding decisions per §27.1

1. **Authenticated model management.** The companion's safe-status surface is
   explicitly not administrative authority. Model management actions travel
   through the authenticated Core management API with an
   installation-administration capability; the settings bridge carries only
   typed action requests and the native layer revalidates authority before
   each call. Generated-contract source is
   `operations.schema.json` (above); changing only a runtime registry is not
   completion.
2. **Core bootstrap/adoption seam.** Not affected: Decision Runtime composes
   under the existing authoritative service and creates no second workspace
   writer. Initial target adoption is out of scope for this capability.
3. **Catalogue amendment.** ADR-042 (this change) proposes the full operation
   list, `decision.1` payload, grants and error posture. Error reason codes
   (§23) are mapped into the canonical error/retry catalogue during
   regeneration — no undeclared strings enter frozen envelopes.
4. **Migration numbering.** Workspace migrations start at 0043; installation
   migrations continue their own sequence inside `installation_migrations.py`.
5. **Local trust limitation.** `LOCAL-IPC-PEER-IDENTITY-DEFERRED` is carried
   forward; socket access is not verified peer identity (§5.4, AT-12).
6. **Model manifest signing.** Uses the release Ed25519 trust-anchor
   mechanism introduced by the c07c signing stage. The production
   trust-anchor document remains a release-owned external prerequisite; the
   model-manifest verification code can land and be exercised with test-only
   anchors, mirroring the onboarding precedent.

## 3. Open decision gates (blocking order)

| Gate | Blocker | Owner |
|---|---|---|
| G-1 | ADR-042 acceptance (this PR) | Owner |
| G-2 | Laya 0.1.1 packaging source: approved internal distribution from pinned `4619e048`, or verified public wheel | Owner + packaging |
| G-3 | Authorisation to reuse the release trust-anchor mechanism for signed model manifests | Release owner |
| G-4 | Model/tokeniser/dependency licence BOM and redistribution review | Owner |
| G-5 | Demonstrated worker network denial + filesystem containment on a signed build | Owner + packaging |

## 4. Execution order

PR slicing, dependency graph, per-PR acceptance-test mapping and risk
register are maintained in the companion implementation plan
(`decision-runtime-implementation-plan-v1.0.md`). PR-1 is this change.
Work that does not require the catalogue amendment (worker packaging spike,
preparer parity fixtures) proceeds in an isolated experimental area and
cannot establish a public capability by accident.

## 5. Stop conditions

As specified in §10 of SPEC-CORE-DEC-001: no UI claiming creation/adoption
before the runtime seam is demonstrated; no raw-path APIs in onboarding or
decision surfaces; no simulated providers in production paths; recorded
contract gaps (omnivia-core issues #118–120) remain the authoritative
tracking for the not-yet-built feature seams.
