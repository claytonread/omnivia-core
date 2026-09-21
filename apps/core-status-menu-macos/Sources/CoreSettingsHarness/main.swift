// ============================================================================
// Developer-only harness: opens the Core Settings window directly, without the
// status-menu companion. Not a product surface; used to exercise the screen
// during development. Run: swift run omnivia-core-settings-harness
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

    // Visual-fidelity host: renders the Core Settings design package itself.
    // Pass `--native` to open the AppKit-rendered window instead.
    let useNative = CommandLine.arguments.contains("--native")
    if useNative {
        let coordinator = ReadinessCoordinator(
            context: ReadinessContext(
                installationRevision: "harness-install",
                localSessionRef: UUID().uuidString,
                targetRef: "local",
                workspaceRef: "harness-workspace",
                configurationRevision: "1"
            )
        )
        let controller = SettingsWindowController(coordinator: coordinator)
        controller.attentionNotificationsSelected = true
        controller.openOrFocus()
    } else {
        PrototypeSettingsWindowController().openOrFocus()
    }
    application.run()
}
