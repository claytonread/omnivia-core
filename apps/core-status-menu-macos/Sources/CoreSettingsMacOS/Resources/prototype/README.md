# OmniVia Core Settings — prototype package

Open `Core-Settings.html` directly (no server needed). Everything is local.

## Contents
- `Core-Settings.html` — the window (with the development harness beneath it)
- `core-settings/` — page CSS, fixture model (`core-fixtures.js`), macOS readiness model (`core-mac.js`), presentation helpers, panes, actions, shell
- `styles/`, `lib/ov-ui/tokens/`, `os/`, `ai-settings/`, `settings-window/` — the shared OmniVia token layer and settings-shell stylesheets this window reuses, unchanged
- `lib/ov-icons-local.js` — offline icon renderer (Lucide geometry, SF Symbols intent)
- `icons/` — every glyph the window uses, as individual 24px SVGs (`currentColor`, 1.6 stroke) for the native SF Symbol mapping
- `assets/` — OmniVia mark, wordmark, app icon, favicon
- `HANDOFF.md` — reused vs new components, assumptions, prototype-only behaviour, native integration points to verify

## Icon → SF Symbol mapping (design intent)
sliders-horizontal → slider.horizontal.3 · hard-drive → internaldrive · refresh-cw → arrow.triangle.2.circlepath · shield → shield · wrench → wrench.adjustable · check → checkmark · check-circle-2 → checkmark.circle · alert-circle → exclamationmark.circle · alert-triangle → exclamationmark.triangle · circle-dashed → circle.dashed · circle-slash → circle.slash · clock → clock · info → info.circle · external-link → arrow.up.right.square · cloud-off → icloud.slash · power → power · shield-check → checkmark.shield · plus → plus · chevron-right → chevron.right · x → xmark

All fixture data is synthetic. Nothing in the prototype touches login items, files, services or the network.
