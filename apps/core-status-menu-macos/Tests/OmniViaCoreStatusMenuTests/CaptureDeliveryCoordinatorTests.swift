import CryptoKit
import DocumentIntakeMacOS
import Foundation
import XCTest

final class CaptureDeliveryCoordinatorTests: XCTestCase {
    private final class Transport: @unchecked Sendable, CaptureDeliveryTransport {
        let descriptorDigest = "sha256:" + String(repeating: "b", count: 64)
        let outcome: CaptureDeliveryOutcome
        private let lock = NSLock()
        private var documents: [Data] = []

        init(_ outcome: CaptureDeliveryOutcome) { self.outcome = outcome }

        func deliver(input: Data, operationKey: String) async -> CaptureDeliveryOutcome {
            lock.withLock { documents.append(input) }
            return outcome
        }

        func delivered() -> [Data] { lock.withLock { documents } }
    }

    func testPendingEntryIsEncodedDeliveredAndReconciledAsAdmitted() async throws {
        let root = try temporaryDirectory()
        let queue = try CaptureIntentQueue(root: root)
        let payload = Data("hello".utf8)
        let intent = try makeIntent(payload: payload)
        try queue.enqueue(intent: intent, payload: payload)
        let captureID = "dcap-" + String(repeating: "a", count: 64)
        let transport = Transport(.admitted(captureID: captureID))
        let delivery = CaptureDeliveryCoordinator(queue: queue, transport: transport)

        let outcomes = await delivery.deliverPending()

        XCTAssertEqual(outcomes.map(\.state), [.admitted])
        XCTAssertEqual(outcomes[0].admittedCaptureID, captureID)
        let wire = try XCTUnwrap(
            JSONSerialization.jsonObject(with: try XCTUnwrap(transport.delivered().first))
                as? [String: Any]
        )
        XCTAssertEqual(wire["document_id"] as? String, "doc-1")
        XCTAssertEqual(wire["content_base64"] as? String, payload.base64EncodedString())
        XCTAssertEqual(wire["media_type"] as? String, "text/plain")
        XCTAssertThrowsError(try queue.payloadURL(for: intent.itemId))
    }

    func testUncertainDeliveryPreservesRecoveryIdentity() async throws {
        let root = try temporaryDirectory()
        let queue = try CaptureIntentQueue(root: root)
        let payload = Data("hello".utf8)
        let intent = try makeIntent(payload: payload)
        try queue.enqueue(intent: intent, payload: payload)
        let delivery = CaptureDeliveryCoordinator(queue: queue, transport: Transport(.unknown))

        let outcomes = await delivery.deliverPending()

        XCTAssertEqual(outcomes.map(\.state), [.unknownOutcome])
        XCTAssertEqual(outcomes[0].attemptGeneration, 1)
        XCTAssertNotNil(outcomes[0].stageRequestDigest)
        XCTAssertNotNil(outcomes[0].stageDescriptorDigest)
        XCTAssertNoThrow(try queue.payloadURL(for: intent.itemId))
    }

    private func makeIntent(payload: Data) throws -> CaptureIntentV3 {
        let digest = "sha256:" + SHA256.hash(data: payload).map { String(format: "%02x", $0) }.joined()
        return try CaptureIntentV3(
            submissionId: "submission-1",
            itemId: "item-1",
            targetRef: "local",
            workspaceRef: "workspace-1",
            targetBindingRevision: "1",
            callerContextBinding: CallerContextBinding(
                clientContextRef: "macos-companion",
                principalRef: "native-user",
                contextRevision: "1"
            ),
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
