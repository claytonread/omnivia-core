import AppKit
import CoreCaptureMacOS
import DocumentIntakeMacOS
import Foundation

try MainActor.assumeIsolated {
    let application = NSApplication.shared
    application.setActivationPolicy(.regular)
    let stateRoot = FileManager.default.temporaryDirectory
        .appendingPathComponent("omnivia-core-capture-harness", isDirectory: true)
    let submitter = try ManualCaptureSubmission(
        stateRoot: stateRoot,
        workspaceRef: "harness-workspace"
    )
    let controller = CapturePanelController(
        workspaceName: "Harness workspace",
        submitter: submitter
    )
    controller.openOrFocus()
    application.run()
}
