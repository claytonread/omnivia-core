import AppKit
import CoreCaptureMacOS
import DocumentIntakeMacOS
import Foundation

try MainActor.assumeIsolated {
    let application = NSApplication.shared
    application.setActivationPolicy(.regular)
    let stateRoot = FileManager.default.temporaryDirectory
        .appendingPathComponent("omnivia-core-capture-harness", isDirectory: true)
    let environment = try DocumentIntakeEnvironment(stateRoot: stateRoot)
    let submitter = ManualCaptureSubmission(
        environment: environment,
        workspaceRef: "harness-workspace"
    )
    let folderSubmitter = FolderCaptureSubmission(
        environment: environment,
        workspaceRef: "harness-workspace"
    )
    let controller = CapturePanelController(
        workspaceName: "Harness workspace",
        submitter: submitter,
        folderSubmitter: folderSubmitter
    )
    controller.openOrFocus()
    application.run()
}
