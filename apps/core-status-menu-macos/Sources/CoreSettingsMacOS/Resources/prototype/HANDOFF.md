# OmniVia Core Settings — handoff notes (prototype)

Prototype: `OmniVia/Core-Settings.html` (+ `core-settings/`). Design brief: `uploads/omnivia-core-settings-claude-design-prompt-v1.0.md` v1.0.

## Reused
- Settings shell: `settings-window/settings-window.css` (source list, rows, groups, segmented, toasts, stub), owned-window chrome grammar (10px corner, window shadow, single close control) from `os/owned-window.js`.
- Controls: `.ov-btn*`, `.ov-toggle`, `.ov-field`, `.ov-pill`, `.ai-health/.ai-dot` (styles/app.css, ai-settings.css); sheets, disclosures, `.ai-dl`, `.ai-code`, `.ai-field` from `ai-settings/ai-settings.css`.
- Brand: `assets/omnivia-mark.svg` geometry inline, mark + "OmniVia" treatment as in `os/next-shell.js` titlebar; "Core Settings" as secondary text.
- Icons: `lib/ov-icons-local.js` (offline Lucide stand-in for SF Symbols).

## New component candidates
- `cs-head` branded window header (60px) with product identity + close.
- `cs-foot` sidebar service-status line (one place only).
- `cs-path` truncating display path with Copy (display data, not a setting).
- `cs-select` — a small `<select>` on the `.ov-field` tokens; the shell has no select primitive.
- `cs-disc` inline disclosure for row groups; `cs-app-d` expanded application details.
- `cs-progress` thin progress track (index rebuild).

## Visual assumptions
- 900×640 window, 200px sidebar, 24px pane padding, 60px header (brief fallbacks; no Core reference measurements existed). Pane title 20px (reference 23px) to keep the smaller window calm.
- The reference settings window's "Settings" eyebrow is dropped: the header already names the window.
- Appearance offers System/Light/Dark; the main Settings offers Light/Dark only. System resolves via `prefers-color-scheme`.

## Prototype-only behaviour
- All fixtures live in `core-fixtures.js`; every service-backed change is a `CoreFx.request()` with delay/refusal. Companion prefs use `CoreFx.local()` and apply immediately.
- Harness (dashed strip under the window): scenarios, "next request fails" flags, request log. Not part of the window.
- "Show in Finder", clipboard, diagnostics export and logs are simulated; the log records the request.
- Last pane and scenario persist in `localStorage` (`ov-core-settings:*`).

## Amendment v1.1 — macOS readiness and recovery (UI-CORE-MAC-READINESS-001)
- Model: `core-mac.js` adds `state.mac` (companion login item, Core startup, notification authorisation, per-source read status, backup destination access, remote connection evidence, Share extension) — separate facts, no single boolean. Passive refresh (`CoreMac.refresh`) runs on window open, pane change, Refresh status and simulated return; it only re-reads and never mutates a result.
- Shared components reused: `.sw-row` supporting area, `.sw-row-sub`, `.ov-btn`, sheets, `.ai-dl`. New variants: `cs-status` (icon + word status: Enabled/Allowed, Off · Optional, Needs approval, Not set up, Needs attention, Not checked, Checking…), `cs-fix` recovery block (title, what is known, effect-specific actions, System Settings route note), `cs-sum` macOS access summary row with Review / Refresh status.
- Placement: General rows carry registration/authorisation state + recovery; one **macOS access** summary group; Data backup destination issue beside the backup control (+ Test backup access in Options); Processing per-source status inside Manage sources with Choose folder again / Open Files & Folders / Check folder access; Access remote-only Connection row with Test connection (timeout ≠ permission denial); Maintenance **macOS check details** disclosure and Share to Core row (installed vs verified).
- Opening System Settings: `sys-open` logs the route, shows the manual path, never grants; launch failure shows the manual route + Try again with the result unchanged. Harness buttons simulate return without (11) / with (12) a change.
- Fixtures: MAC-UI-01…10, 10b, 13…17 selectable in the harness; 11/12 are the return buttons; 18 is workspace switch during a check (token invalidates the old result). Harness is marked **Prototype · simulated macOS state**; no browser permission APIs are used.
- Native integration points to verify: `SMAppService.status` / `openSystemSettingsLoginItems()`, `UNUserNotificationCenter.getNotificationSettings`, security-scoped bookmark read for sources (Core reader vs companion), destination read/write probe with temp file, Local Network denial detection (qualified result only), Share extension enumeration, exact System Settings URL routes per macOS version. Not implemented/claimed: Full Disk Access status, firewall audit, any voice/accessibility permissions.

- Login item mechanism for "Start Core at login" and its approval state (SMAppService?).
- Whether Core exposes: backup destination/schedule/retention, resource profiles, battery pause, pause/resume, per-app access levels with authorisation acknowledgement, revoke, index rebuild with progress, diagnostics bundle contents, an installer update-check route.
- Contribute-level access (`Read and contribute`) is capability-gated: the current MCP implementation is not claimed to expose it.
- Remote-target variant: which values are readable and which lifecycle actions exist.

## Needs verification (backend / installation)
