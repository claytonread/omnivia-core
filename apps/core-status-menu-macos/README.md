# OmniVia Core Status Menu for macOS

This Swift Package is the optional native lifecycle companion for the headless
OmniVia Core Service. It is a **thin status and control client**: it owns no
workspace, lease, database, process identity, endpoint, or lifecycle logic, it
decides nothing about Core's state, and it offers no control Core has not told
it that it may offer. It only invokes the installed
`omnivia start|stop|status --json` adapter and renders what comes back. It is a
separate process by design: stopping Core must not also remove the control that
can start Core again.

The source target is macOS 13 or later. That baseline provides the AppKit and SF
Symbols behavior used for a native dark-mode/Retina menu-bar icon while keeping
the package dependency-free.

## Build and test

From the Core repository root:

```bash
swift test --package-path apps/core-status-menu-macos
swift build --package-path apps/core-status-menu-macos
```

Run the development executable with the default Core installation
(`~/.omnivia`) and the fixed `omnivia` executable lookup:

```bash
swift run --package-path apps/core-status-menu-macos omnivia-core-status-menu
```

For a reviewed development checkout, explicit absolute overrides are available:

```bash
swift run --package-path apps/core-status-menu-macos omnivia-core-status-menu \
  --cli /absolute/path/to/omnivia \
  --home /absolute/path/to/core-installation
```

The executable resolution order is:

1. explicit absolute `--cli` override;
2. `Contents/Resources/omnivia` in a packaged app;
3. `omnivia` beside the companion executable;
4. `/opt/homebrew/bin/omnivia` and `/usr/local/bin/omnivia`;
5. the fixed `omnivia` name on the operating system `PATH`.

The Core repository intentionally forbids OmniVia-specific environment variables
from redirecting state paths or executable names, so the companion does not read
`OMNIVIA_HOME`, `OMNIVIA_CLI`, or equivalents. `--home` and `--cli` are explicit,
reviewable process arguments and are intended primarily for development and
packaging verification.

## Behavior

- Starts at **Checking…** and probes live Core readiness immediately.
- Polls every five seconds without overlapping lifecycle commands.
- Shows **Starting…** and **Stopping…** while bounded subprocess operations run.
- Keeps Refresh, Start Service, Stop Service, Show Service Log, and Quit Status
  Menu in stable menu positions.
- Reveals `<home>/run/service.log` without creating installation state.
- Quitting the status menu leaves the Core Service unchanged.

## Lifecycle adapter version 2

The companion reads `"lifecycle_adapter_version": 2` and nothing else: version 1
published a raw `service` object and a free-form `reason`, and a version 1
document — like any other version — is refused rather than read.

A version 2 document carries `action`, `ok`, `outcome`, a required `code` from
the adapter's closed set, and an optional `safe_status`. `safe_status` is a
canonical `CoreSafeStatusV1` (ADR-038), mirrored here as closed Swift enums and
bounded scalars in `CoreSafeStatus.swift`. The document is accepted only when:

- the adapter version is exactly 2;
- every required field is present and every closed value — target kind and
  management, the four normalized states, warning codes, permitted actions, the
  lifecycle `code` — is one this build knows;
- `code`, `action`, `outcome` and `ok` form one of the adapter's exact valid
  tuples: a code that merely belongs to the right action is not enough, so a
  `status_not_running` reporting `failed`, or a `stop_stopped` reporting
  `ok: false`, is refused;
- the status and its target agree on `contract_version`, and it is exactly the
  Application Contract version this build mirrors (`1.3`) — another minor is
  refused as readily as another major;
- every object carries only the keys its canonical schema declares — the
  envelope, the safe status, and its nested target are all closed;
- every scalar matches its declared value domain and bounds (`Identifier` and
  `WorkspaceId` patterns, `ContractVersion`, `ReleaseVersion`, a 1–256 character
  `display_name`);
- `warning_codes` and `permitted_actions` are within their caps and repeat
  nothing;
- `start`/`stop`/`restart` appear only for a `local`, `locally_managed` target.

Anything else fails closed: the whole document is refused and the menu shows a
fixed *status unavailable* line with **no controls**. `safe_status` may also be
legitimately absent — when the adapter could form no valid target — which also
means no controls.

## The privacy boundary

The companion is the least-privileged consumer Core has: it has authenticated
nothing, so it is shown, and shows, only what a safe status may carry.

- Menu state and every rendered word come from the safe status alone. Start and
  Stop are enabled **only** by `permitted_actions`, never by the lifecycle
  phase. There is no Restart item; a status that offers `restart` still moves no
  control. A remote or externally-managed target can therefore never present a
  local process control.
- Nothing the adapter wrote is ever displayed. No stderr, no `reason`, no
  decoder complaint, no `localizedDescription`, no path, endpoint, pid, or
  credential. The adapter's stderr is drained only so a chatty command cannot
  deadlock on a full pipe, and is discarded unread.
- Every degraded, unknown or failure phrase is a fixed local string chosen from
  a closed enum — a `CoreSafeWarningCode`, a normalized state, or one of the
  companion's own closed runner failures. `display_name` is validated but never
  rendered; the menu's words are this build's own.

## Non-goals in this source slice

- no service auto-start or auto-restart;
- no launch-at-login, LaunchAgent, daemon, or reboot persistence;
- no port scanning, process-name scanning, or pid-based controls;
- no telemetry, notifications, network calls, or settings UI;
- no signing, notarization, installer, or Platform cutover yet.

Packaging, signing, installation lifecycle, CI integration, and removal of the
legacy Platform status menu remain staged follow-up work after this source and
its lifecycle contract are accepted.
