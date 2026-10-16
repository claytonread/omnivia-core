import CryptoKit
import DocumentIntakeMacOS
import Foundation
import XCTest
@testable import OmniViaCoreStatusMenu

/// AT-18: a crash after the Core commit but before the local receipt, then
/// same-key reconciliation, produces one canonical effect.
///
/// Unlike the coordinator tests (a fake transport) and the Python vertical tests
/// (no companion), this joins both halves: the real durable `CaptureIntentQueue`,
/// the real `DocumentCaptureWireEncoder` and `CLICaptureDelivery`, the installed
/// `omnivia` CLI, and a live `omnivia-core-service` over a unix socket. The only
/// simulated thing is the lost receipt itself.
final class CaptureCoreBoundaryTests: XCTestCase {
    /// Delegates to the real CLI delivery, then drops its answer: Core has
    /// committed, and the companion never learns it. That is the state a crash
    /// between the CLI's exit and `markAdmitted` leaves on disk.
    private final class LostReceiptTransport: CaptureDeliveryTransport, @unchecked Sendable {
        let real: CLICaptureDelivery
        var descriptorDigest: String { real.descriptorDigest }
        var deliveryBinding: CaptureDeliveryBinding { real.deliveryBinding }
        private let lock = NSLock()
        private var delivered: [(key: String, outcome: CaptureDeliveryOutcome)] = []

        init(_ real: CLICaptureDelivery) { self.real = real }

        var committed: [(key: String, outcome: CaptureDeliveryOutcome)] { lock.withLock { delivered } }

        func deliver(input: Data, payloadURL: URL, operationKey: String) async -> CaptureDeliveryOutcome {
            let outcome = await real.deliver(input: input, payloadURL: payloadURL, operationKey: operationKey)
            lock.withLock { delivered.append((operationKey, outcome)) }
            return .unknown
        }
    }

    private struct Core {
        let cli: URL
        let installationState: URL
        let workspaceID: String
        var delivery: CLICaptureDelivery {
            CLICaptureDelivery(
                executable: cli,
                installationState: installationState,
                deliveryBinding: .installedCompanion(
                    installationState: installationState,
                    workspaceRef: workspaceID
                )
            )
        }
    }

    func testRestartedCoordinatorReconcilesALostReceiptOnTheRetainedKeyToOneCapture() async throws {
        let core = try startCore()
        let root = try temporaryDirectory()
        let queue = try CaptureIntentQueue(root: root)
        let payload = Data("hello".utf8)
        let intent = try makeIntent(payload: payload, binding: core.delivery.deliveryBinding)
        try queue.enqueue(intent: intent, payload: payload)

        // Core commits; the receipt is lost.
        let lost = LostReceiptTransport(core.delivery)
        let attempt = await CaptureDeliveryCoordinator(queue: queue, transport: lost).deliverPending()
        XCTAssertEqual(attempt.map(\.state), [.unknownOutcome])
        let sent = try XCTUnwrap(lost.committed.first)
        guard case let .admitted(captureID) = sent.outcome else {
            return XCTFail("Core did not commit the first attempt: \(sent.outcome)")
        }
        XCTAssertEqual(try captureIDs(core, documentID: "doc-1"), [captureID])

        // Restart: a new queue over the same durable root retains the key Core saw.
        let restarted = try CaptureIntentQueue(root: root)
        let entry = try XCTUnwrap(restarted.allEntries().first)
        XCTAssertEqual(entry.state, .unknownOutcome)
        let retained = try XCTUnwrap(entry.attempts.last)
        XCTAssertEqual(retained.operationKey, sent.key)

        // The production startup path: the coordinator replays the frozen
        // request under that same key and settles the entry on the admitted
        // answer. Nothing here sends, stages or settles by hand.
        let settled = await CaptureDeliveryCoordinator(queue: restarted, transport: core.delivery)
            .deliverPending()
        let admitted = try XCTUnwrap(settled.first)
        XCTAssertEqual(settled.count, 1)
        XCTAssertEqual(admitted.state, .admitted)
        XCTAssertEqual(admitted.admittedCaptureID, captureID)
        XCTAssertEqual(admitted.attemptGeneration, entry.attemptGeneration)
        XCTAssertEqual(admitted.attempts.map(\.operationKey), [sent.key])
        XCTAssertEqual(try XCTUnwrap(restarted.allEntries().first).state, .admitted)

        // One canonical effect on the Core side.
        XCTAssertEqual(try captureIDs(core, documentID: "doc-1"), [captureID])
    }

    /// Control: the same lost receipt reconciled under a *different* key makes a
    /// second capture, so the assertion above can actually see a duplicate.
    func testReconcilingUnderAFreshKeyWouldHaveDuplicatedTheCapture() async throws {
        let core = try startCore()
        let root = try temporaryDirectory()
        let queue = try CaptureIntentQueue(root: root)
        let payload = Data("hello".utf8)
        let intent = try makeIntent(payload: payload, binding: core.delivery.deliveryBinding)
        try queue.enqueue(intent: intent, payload: payload)
        _ = await CaptureDeliveryCoordinator(queue: queue, transport: LostReceiptTransport(core.delivery))
            .deliverPending()
        let entry = try XCTUnwrap(queue.allEntries().first)

        let other = await core.delivery.deliver(
            input: try DocumentCaptureWireEncoder().encode(entry: entry),
            payloadURL: try queue.payloadURL(for: intent.itemId),
            operationKey: CaptureAttemptRecord.childOperationKey(parent: intent.operationKey, generation: 2)
        )

        guard case .admitted = other else { return XCTFail("fresh-key delivery failed: \(other)") }
        XCTAssertEqual(try captureIDs(core, documentID: "doc-1").count, 2)
    }

    // MARK: - Harness

    /// Initialises a workspace and starts a live service on a unix socket. Uses
    /// the `omnivia` found on PATH and the service beside it, so a stray
    /// `omnivia-core-service` elsewhere on PATH is never picked up.
    private func startCore() throws -> Core {
        guard let cli = executableOnPath("omnivia") else { throw XCTSkip(missingToolchain) }
        let service = cli.deletingLastPathComponent().appendingPathComponent("omnivia-core-service")
        try XCTSkipUnless(FileManager.default.isExecutableFile(atPath: service.path), missingToolchain)

        // Unix socket paths are short; the ambient TMPDIR is too deep for one.
        let directory = URL(fileURLWithPath: "/tmp/at18-\(UUID().uuidString.prefix(8))", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        let workspace = directory.appendingPathComponent("workspace")
        let installation = directory.appendingPathComponent("installation-state")
        let common = ["--workspace", workspace.path, "--installation-state", installation.path]

        let initialised = try run(service, common + ["--init"])
        let workspaceID = try XCTUnwrap(
            ((initialised["workspace"]) as? [String: Any])?["workspace_id"] as? String
        )

        let process = Process()
        let output = Pipe()
        process.executableURL = service
        process.arguments = common + ["--endpoint", "unix://\(directory.appendingPathComponent("core.sock").path)"]
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        addTeardownBlock {
            process.terminate()
            process.waitUntilExit()
        }
        // The service prints one readiness document; block on its pipe, no polling.
        var seen = Data()
        while true {
            let chunk = output.fileHandleForReading.availableData
            guard !chunk.isEmpty else { throw HarnessError.serviceExited }
            seen.append(chunk)
            if let document = try? JSONSerialization.jsonObject(with: seen) as? [String: Any] {
                guard document["ready"] as? Bool == true else { throw HarnessError.serviceExited }
                break
            }
        }
        return Core(cli: cli, installationState: installation, workspaceID: workspaceID)
    }

    private var missingToolchain: String {
        "omnivia and omnivia-core-service must be installed side by side on PATH"
    }

    private enum HarnessError: Error { case serviceExited, commandFailed(String) }

    private func executableOnPath(_ name: String) -> URL? {
        let found = (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":")
            .map { URL(fileURLWithPath: String($0)).appendingPathComponent(name) }
            .first { FileManager.default.isExecutableFile(atPath: $0.path) }
        // A CI row that lost the toolchain must fail, not skip green.
        if found == nil, ProcessInfo.processInfo.environment["CI"] != nil {
            XCTFail(missingToolchain)
        }
        return found
    }

    private func run(_ executable: URL, _ arguments: [String]) throws -> [String: Any] {
        let process = Process()
        let output = Pipe()
        process.executableURL = executable
        process.arguments = arguments
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        let data = try output.fileHandleForReading.readToEnd() ?? Data()
        process.waitUntilExit()
        guard process.terminationStatus == 0,
              let document = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { throw HarnessError.commandFailed(arguments.joined(separator: " ")) }
        return document
    }

    /// The capture ids Core holds for a document, read back through its own
    /// `document history` operation rather than by inspecting its storage.
    private func captureIDs(_ core: Core, documentID: String) throws -> [String] {
        let document = try run(core.cli, [
            "--installation-state", core.installationState.path,
            "--workspace-id", core.workspaceID,
            "document", "history",
            "--input-json", "{\"document_id\":\"\(documentID)\"}",
            "--json",
        ])
        let result = try XCTUnwrap(document["result"] as? [String: Any])
        return try XCTUnwrap(result["captures"] as? [[String: Any]]).compactMap { $0["capture_id"] as? String }
    }

    private func makeIntent(payload: Data, binding: CaptureDeliveryBinding) throws -> CaptureIntentV3 {
        let digest = "sha256:" + SHA256.hash(data: payload).map { String(format: "%02x", $0) }.joined()
        return try CaptureIntentV3(
            submissionId: "submission-1",
            itemId: "item-1",
            targetRef: binding.targetRef,
            workspaceRef: binding.workspaceRef,
            targetBindingRevision: binding.targetBindingRevision,
            callerContextBinding: binding.callerContextBinding,
            surface: .manual,
            contentKind: .text,
            payloadRef: "payload-1",
            byteLength: UInt64(payload.count),
            localIntegrityDigest: digest,
            requestedSourceIdentity: "source-1",
            operationKey: "capture-1",
            intentKind: .newCapture,
            basePrecondition: .create,
            processingProfile: ProcessingProfileBinding(profileRef: "document-default", revision: "1"),
            originalBinding: OriginalBindingIntent(
                accessRecordId: "access-1",
                bindingGeneration: 1,
                linkedDocumentId: "doc-1"
            ),
            createdAt: "2026-10-14T00:00:00Z",
            displayName: "note.txt"
        )
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
