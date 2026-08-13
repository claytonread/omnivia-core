# OmniVia Core Status Menu for macOS

This Swift Package is the optional native lifecycle companion for the headless
OmniVia Core Service. It is a separate process by design: stopping Core must not
also remove the control that can start Core again. The companion owns no
workspace, lease, database, process identity, or service lifecycle logic. It
only invokes the installed `omnivia start|stop|status --json` adapter.

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

The lifecycle adapter document is versioned with
`"lifecycle_adapter_version": 1`. Its stable fields are `action`, `ok`,
`outcome`, optional `service`, and optional `reason`. The service snapshot is
limited to workspace id, service instance id, live lifecycle state, readiness,
and unmet reasons. It deliberately contains no endpoint or pid.

## Non-goals in this source slice

- no service auto-start or auto-restart;
- no launch-at-login, LaunchAgent, daemon, or reboot persistence;
- no port scanning, process-name scanning, or pid-based controls;
- no telemetry, notifications, network calls, or settings UI;
- no signing, notarization, installer, or Platform cutover yet.

Packaging, signing, installation lifecycle, CI integration, and removal of the
legacy Platform status menu remain staged follow-up work after this source and
its lifecycle contract are accepted.
