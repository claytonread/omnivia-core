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
