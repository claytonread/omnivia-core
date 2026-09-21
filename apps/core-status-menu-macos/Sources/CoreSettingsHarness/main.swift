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
    controller.startCoreAtLoginSelected = false
    controller.attentionNotificationsSelected = true
    controller.hasWorkspace = false
    controller.openOrFocus()
    application.run()
}
