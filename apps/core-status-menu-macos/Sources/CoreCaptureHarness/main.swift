import AppKit
import CoreCaptureMacOS
import CryptoKit
import Darwin
import DocumentIntakeMacOS
import Foundation

private func digest(_ data: Data) -> String {
    "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

private func writeReady(_ url: URL, value: String) throws {
    try Data(value.utf8).write(to: url, options: [.atomic])
}

private func waitToBeKilled() -> Never {
    _ = Darwin.raise(SIGSTOP)
    while true { Darwin.pause() }
}

private func queueIntent(payload: Data) throws -> CaptureIntentV3 {
    try CaptureIntentV3(
        submissionId: "durability-submission",
        itemId: "durability-item",
        targetRef: "durability-target",
        workspaceRef: "durability-workspace",
        targetBindingRevision: "durability-binding-1",
        callerContextBinding: CallerContextBinding(
            clientContextRef: "durability-client",
            principalRef: "durability-principal",
            contextRevision: "durability-context-1"
        ),
        surface: .manual,
        contentKind: .pdf,
        payloadRef: "durability-payload",
        byteLength: UInt64(payload.count),
        localIntegrityDigest: digest(payload),
        requestedSourceIdentity: "durability-source",
        operationKey: "durability-operation",
        intentKind: .newCapture,
        basePrecondition: .create,
        processingProfile: .documentDefault,
        createdAt: "2026-10-18T00:00:00Z",
        displayName: "durability.pdf"
    )
}

private func runDurabilityHarness(_ arguments: [String]) throws -> Never {
    guard arguments.count == 4 else { throw CocoaError(.fileReadInvalidFileName) }
    let mode = arguments[1]
    let root = URL(fileURLWithPath: arguments[2], isDirectory: true)
    let ready = URL(fileURLWithPath: arguments[3], isDirectory: false)
    let payload = Data("durable-payload".utf8)
    switch mode {
    case "share-before":
        let inbox = try ShareInboxStore(root: root)
        let request = try inbox.beginRequest()
        let relative = ShareInboxStore.payloadName(index: 0)
        try payload.write(to: inbox.payloadDestination(request, relativePath: relative))
        try writeReady(ready, value: request.requestID)
    case "share-after":
        let inbox = try ShareInboxStore(root: root)
        let request = try inbox.beginRequest()
        let relative = ShareInboxStore.payloadName(index: 0)
        try payload.write(to: inbox.payloadDestination(request, relativePath: relative))
        let item = ShareInboxItemV1(
            slotID: UUID().uuidString.lowercased(),
            displayName: "durability.txt",
            relativePath: relative,
            kind: .file,
            byteLength: UInt64(payload.count),
            digest: digest(payload)
        )
        try inbox.publish(ShareInboxEnvelopeV1(
            requestID: request.requestID,
            senderBundleID: NativeHandoffMailbox.shareBundleID,
            createdAt: Date(timeIntervalSince1970: 1_797_638_400),
            items: [item]
        ))
        try writeReady(ready, value: request.requestID)
    case "queue-before":
        let queue = try CaptureIntentQueue(root: root)
        _ = try queue.prepareCandidate([(intent: queueIntent(payload: payload), payload: payload)])
        try writeReady(ready, value: "durability-item")
    case "queue-after":
        let queue = try CaptureIntentQueue(root: root)
        _ = try queue.enqueue(intent: queueIntent(payload: payload), payload: payload)
        try writeReady(ready, value: "durability-item")
    default:
        throw CocoaError(.featureUnsupported)
    }
    waitToBeKilled()
}

private func inspectDurabilityStore(_ arguments: [String]) throws {
    guard arguments.count == 3 else { throw CocoaError(.fileReadInvalidFileName) }
    let mode = arguments[1]
    let root = URL(fileURLWithPath: arguments[2], isDirectory: true)
    let result: [String: Any]
    switch mode {
    case "share":
        let inbox = try ShareInboxStore(root: root)
        let pending = inbox.pendingRequestIDs()
        result = [
            "pending": pending,
            "complete": pending.allSatisfy { inbox.envelope(requestID: $0) != nil },
        ]
    case "queue":
        let queue = try CaptureIntentQueue(root: root)
        let entries = queue.allEntries()
        result = [
            "items": entries.map(\.intent.itemId),
            "states": entries.map(\.state.rawValue),
            "payloadsReadable": entries.allSatisfy {
                (try? Data(contentsOf: queue.payloadURL(for: $0.intent.itemId))) == Data("durable-payload".utf8)
            },
        ]
    default:
        throw CocoaError(.featureUnsupported)
    }
    let encoded = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    FileHandle.standardOutput.write(encoded)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

let arguments = CommandLine.arguments
if arguments.count > 1, arguments[1] == "durability" {
    try runDurabilityHarness(Array(arguments.dropFirst()))
} else if arguments.count > 1, arguments[1] == "inspect-durability" {
    try inspectDurabilityStore(Array(arguments.dropFirst()))
} else {
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
}
