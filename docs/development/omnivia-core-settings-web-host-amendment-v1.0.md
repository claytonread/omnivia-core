# Amendment v1.0 — Web-hosted settings UI for the Core companion

Date: 2026-09-21
Amends: SPEC-CORE-MAC-READINESS-001 (§4.1 components, §5 binding "UI host", §15)
Status: Accepted by owner direction, 2026-09-21 ("Electron-style web UI with a Swift adapter")

## Decision

The Core Settings screen is a **web-rendered settings bundle hosted by the
native companion in a WKWebView**, with a thin Swift adapter — the same
renderer/adapter split Electron uses, minus Node. The bundle is shared with
the Platform settings implementation so both hosts render one design.

Rationale (from the implementation session on branch
`codex/core-settings-screen-20260921`):

1. The OmniVia design system is CSS-first (`tokens.css` is authored for
   Electron webviews); an AppKit recreation is a permanent second
   implementation of every component and did not reach parity after five
   iterations.
2. Platform settings are Electron; one web bundle prevents drift between the
   two hosts.
3. Everything safety-relevant in this feature is already native and stays
   native: the readiness model, passive providers, coordinator, fencing and
   navigator.

## What changes

- §5 binding "UI host": the window is native AppKit and hosts the settings
  bundle in WKWebView; the page is presentation only.
- §4.1: add **SettingsBridge** — the native adapter. It projects typed status
  snapshots into the page and receives typed action requests.

## What does not change

- D02–D04, D09–D10, MR-004/005/011/012/019: the Swift side remains the sole
  authority. Passive refresh never prompts or probes; page script cannot
  execute native actions; every action is allowlisted, opaque-argument only,
  and revalidated (actor, subject, revision, generation) at execution time.
- §11.1 trusted interaction: user intent originates from real clicks in the
  page, forwarded as typed requests; the native gate — not the page — decides
  what runs.
- §13.2: the notification sender and any future registration identity remain
  the native companion; hosting the UI in a webview does not move attribution.
- §20: signed-installation qualification gates are unchanged.

## Bridge contract (v1)

Native → page (status projection, receive-only on the page side):

```json
{
  "type": "statusProjection",
  "schemaVersion": 1,
  "generation": 7,
  "observedAt": "…",
  "summary": { "kind": "ok|noAdditionalAccess|attention|needsVerification|checking",
               "headline": "…", "detail": "…" },
  "checks": [ { "checkId": "core.background", "statusWord": "Off · Optional",
                "tone": "ok|warn|neu|checking", "allowedActions": ["openLoginItems"] } ]
}
```

Page → native (typed action requests; allowlisted, no free-form parameters):

| `action` | Native handler |
| --- | --- |
| `refreshStatus` | `ReadinessCoordinator.scheduleRefresh(.explicitRefresh)` |
| `openLoginItems` | `SettingsNavigator.open(.loginItems)` |
| `openNotifications` | `SettingsNavigator.open(.notifications)` |
| `requestNotifications` | Explicit UNUserNotificationCenter request, then passive re-read |

## Delivery state

- The deterministic native layer and tests are merged on the branch above.
- The bridge and status projection are implemented in
  `Sources/CoreSettingsMacOS/SettingsBridge.swift`; the page-side consumption
  of real projections (replacing the prototype's simulated `mac` state) and
  suppression of the corresponding simulated actions are the next integration
  step, before any release claim (§20).
