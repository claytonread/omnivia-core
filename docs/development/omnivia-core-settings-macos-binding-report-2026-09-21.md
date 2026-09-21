# SPEC-CORE-MAC-READINESS-001 — Phase 0 binding report and implementation record

Date: 2026-09-21
Branch: `codex/core-settings-screen-20260921`
Base: `omnivia-core` `main` at `86423e6` (PR #110 merge)

## 1. Binding findings (§5)

| Binding | Finding |
| --- | --- |
| UI host | Native AppKit menu-bar companion: `apps/core-status-menu-macos/` (SwiftPM, `swift-tools-version: 5.9`, `.macOS(.v13)`), executable `omnivia-core-status-menu`, `NSApplication` accessory policy, `NSStatusItem` menu. There was **no** settings window before this change; the five-pane Core Settings prototype (`OmniVia App UI (39)` package, amendment v1.1) is the presentation reference. |
| Identity | No bundle, no signing, no `SMAppService` declaration exists in this package. Notification sender would be the companion process itself; no helper identities. |
| Installation | Canonical install path comes in via `--installation-state` (`CompanionConfiguration`); no receipts, no updater, no duplicate-copy handling in this package. |
| Startup | **No registration mechanism is implemented.** No SMAppService/launchd plist ownership. Per §19, startup status is reported honestly as unimplemented; no registration, migration or toggle is offered. |
| Core client | `CoreSafeStatusV1` safe-status route, attach-only, pre-authentication surface; lifecycle via `LifecycleCoordinator` + `CLICommandRunner`; singleton `CompanionSingleton` forwards refresh intents. The settings window does not invoke any lifecycle command (MR-005). |
| Sources/backups | Not present in this build. All `source.read:*`, `backup.write:*`, `connection.local-network:*`, `hosting.firewall:*`, `capture.share-extension` adapters are deferred (R2/R3, D08). |
| macOS matrix | Compiles/runs on macOS 13+ SDK 27.0, arm64. Signed-installation qualification is **not** done (see gates). |
| Security | No entitlements/sandbox/notarisation in the SwiftPM package. No TCC, `tccutil`, `defaults`, `launchctl` or private-API use. |

## 2. Implemented (Phases 1 + partial 2 — R1 baseline within binding limits)

New files under `apps/core-status-menu-macos/Sources/OmniViaCoreStatusMenu/Settings/`:

- `ReadinessModel.swift` — closed enums for the seven evidence dimensions (§7.1), typed check IDs with opaque feature refs, reason codes (§7.4), fenced context/snapshot DTOs, native status mappings where unknown native cases degrade to `unsupportedState` (MT-042).
- `ReadinessReducer.swift` — pure applicability resolution (§6.2), aggregation/summary (§7.5: neutral optional states MT-005, unknown blocks blanket success MT-006, startup vs service health separate MT-010).
- `ReadinessProviders.swift` — passive provider protocol with **no effectful surface** (§12.1); qualified notification passive read (`UNUserNotificationCenter.notificationSettings`, never requests); honest unimplemented providers for login/background/Share.
- `ReadinessCoordinator.swift` — generation + context fencing (MT-036), 300 ms coalescing (MT-004), 2 s soft per-provider timeout (MT-003), max 4 concurrent (§12.3), progressive publish, in-memory only (§14.1).
- `SettingsNavigator.swift` — typed destination allowlist (§10.1); `SMAppService.openSystemSettingsLoginItems()` where available, generic `NSWorkspace` System Settings fallback with manual routes; result semantics `navigationRequested`/`genericSettingsOpened`/`launchFailed` (never "access enabled", MT-033).
- `SettingsWindowController.swift` — the five-pane Core Settings window (General / Data / Processing / Access / Maintenance) per I01/I05: one window, opened/focused from the status menu ("Core Settings… ⌘,"), closing never stops Core (MT-001); macOS access summary; startup and notification rows with amendment statuses (Off · Optional / Needs approval / Not checked / Checking…); notification request only via explicit button (MT-013); refresh on open and manual Refresh status.
- `main.swift` — added the `Core Settings…` menu item wiring.

## 3. Test evidence

`swift build` clean; `swift test`: **67 passed, 0 failed** (57 pre-existing + 10 new in `SettingsReadinessTests`), covering MT-003/004/005/006/007/008/009/010/036/042 deterministic cases and the structural MT-002 passive-only assertion.

## 4. Honest gated-capability list (§20.6)

- Startup registration read/mutation: **gated** — no qualified mechanism installed.
- Source/backup/connection/LAN/firewall/Share/FDA adapters: **deferred** (R2/R3).
- Signed-installation qualification, accessibility audit, route-registry qualification: **not performed** — this is a developer-run unsigned build; per §20 no feature-completion claim is made.
- Preflight (`./scripts/preflight`) not yet run on this branch — required before any PR.
