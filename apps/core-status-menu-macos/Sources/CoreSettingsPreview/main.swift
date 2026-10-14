// ============================================================================
// Standalone launcher for the Core Settings window.
//
// Opens the native AppKit settings window on its own, without the status-menu
// companion, so the seven panes can be looked at and driven during
// development. It is not a product surface and ships in no bundle.
//
//   swift run --package-path apps/core-status-menu-macos omnivia-core-settings
//
// It hosts the same `SettingsWindowController` the companion opens, with no
// alternate rendering path: whatever appears here is what the companion shows.
// The context below is this launcher's own, and is visibly synthetic so a
// screenshot taken from it can never be mistaken for a real installation.
// ============================================================================

import AppKit
import Foundation
import CoreSettingsMacOS

// Top-level code runs on the process's main thread; the settings types are
// MainActor-isolated, so state that rather than hopping through an async
// entry point.
try MainActor.assumeIsolated {
    let application = NSApplication.shared
    application.setActivationPolicy(.regular)
    // The brand app icon (Resources/brand/omnivia-appicon.svg).
    if let icon = BrandAssets.image(named: "omnivia-appicon") {
        application.applicationIconImage = icon
    }

    // Optional, so the preview can exercise a real installation:
    //   --cli <path to omnivia> --installation-state <path>
    // Without them the window opens against a synthetic context and the
    // Access pane says it has no route to the CLI, which is the truth.
    func argument(_ name: String) -> String? {
        let args = CommandLine.arguments
        guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
        return args[i + 1]
    }
    var mcp: McpAdministering?
    if let cli = argument("--cli"), let state = argument("--installation-state") {
        mcp = McpCommandRunner(
            executable: URL(fileURLWithPath: cli),
            installationState: URL(fileURLWithPath: state)
        )
    }

    let coordinator = ReadinessCoordinator(
        context: ReadinessContext(
            installationRevision: "preview-install",
            localSessionRef: UUID().uuidString,
            targetRef: "local",
            workspaceRef: "preview-workspace",
            configurationRevision: "1"
        )
    )
    let controller = SettingsWindowController(coordinator: coordinator, mcp: mcp)

    // Optional, so the sidebar footer's service states can be looked at
    // without a companion to report them:
    //   --service-state running|stopped|unreachable|starting|stopping|failed
    // Without it the footer says it has no feed, which is the truth here.
    if let name = argument("--service-state") {
        let state: CoreServiceState?
        switch name {
        case "running": state = .running
        case "stopped": state = .stopped
        case "starting": state = .starting
        case "stopping": state = .stopping
        case "failed": state = .failed
        case "unreachable":
            // A fixed time, so this launcher's output is reproducible.
            var when = Calendar.current.dateComponents([.year, .month, .day], from: Date())
            when.hour = 9
            when.minute = 41
            state = .unreachable(lastSeen: Calendar.current.date(from: when))
        default: state = nil
        }
        guard let state else {
            FileHandle.standardError.write(Data("unknown --service-state \(name)\n".utf8))
            exit(2)
        }
        controller.updateServiceState(state, host: "This Mac")
    }

    controller.openOrFocus()
    application.run()
}
