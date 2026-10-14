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
    controller.openOrFocus()
    application.run()
}
