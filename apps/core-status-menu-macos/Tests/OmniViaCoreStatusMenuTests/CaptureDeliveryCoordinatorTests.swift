import CryptoKit
import DocumentIntakeMacOS
import Foundation
import XCTest

final class CaptureDeliveryCoordinatorTests: XCTestCase {
    private final class Transport: @unchecked Sendable, CaptureDeliveryTransport {
        let descriptorDigest: String
        let deliveryBinding: CaptureDeliveryBinding
        let outcome: CaptureDeliveryOutcome
        private let lock = NSLock()
        private var documents: [Data] = []
        private var payloads: [URL] = []
        private var operationKeys: [String] = []

        init(
            _ outcome: CaptureDeliveryOutcome,
            binding: CaptureDeliveryBinding = .companion(workspaceRef: "workspace-1"),
            descriptorDigest: String = "sha256:" + String(repeating: "b", count: 64)
        ) {
            self.outcome = outcome
            self.descriptorDigest = descriptorDigest
            deliveryBinding = binding
        }

        func deliver(input: Data, payloadURL: URL, operationKey: String) async -> CaptureDeliveryOutcome {
            lock.withLock {
                documents.append(input)
                payloads.append(payloadURL)
                operationKeys.append(operationKey)
            }
            return outcome
        }

        func delivered() -> [Data] { lock.withLock { documents } }
        func deliveredPayloads() -> [URL] { lock.withLock { payloads } }
        func deliveredOperationKeys() -> [String] { lock.withLock { operationKeys } }
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
        XCTAssertNil(wire["content_base64"])
        XCTAssertEqual(wire["media_type"] as? String, "text/plain")
        XCTAssertEqual(transport.deliveredPayloads().count, 1)
        XCTAssertEqual(transport.deliveredOperationKeys(), outcomes[0].attempts.map(\.operationKey))
        XCTAssertNotEqual(transport.deliveredOperationKeys().first, intent.operationKey)
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

    // MARK: - Same-key reconciliation of an unknown outcome

    /// A queue holding one entry whose first attempt ended unknown, as a
    /// restart leaves it.
    private func unknownEntry() async throws -> (
        queue: CaptureIntentQueue, entry: CaptureQueueEntry, payload: URL
    ) {
        let queue = try CaptureIntentQueue(root: try temporaryDirectory())
        let payload = Data("hello".utf8)
        let intent = try makeIntent(payload: payload)
        try queue.enqueue(intent: intent, payload: payload)
        _ = await CaptureDeliveryCoordinator(queue: queue, transport: Transport(.unknown)).deliverPending()
        let entry = try XCTUnwrap(queue.allEntries().first)
        XCTAssertEqual(entry.state, .unknownOutcome)
        return (queue, entry, try queue.payloadURL(for: intent.itemId))
    }

    private func assertUnchanged(
        _ queue: CaptureIntentQueue,
        _ entry: CaptureQueueEntry,
        line: UInt = #line
    ) {
        XCTAssertEqual(queue.allEntries(), [entry], line: line)
        XCTAssertNoThrow(try queue.payloadURL(for: entry.intent.itemId), line: line)
    }

    func testAdmittedReplaySettlesTheUnknownEntryOnItsOwnKey() async throws {
        let (queue, entry, _) = try await unknownEntry()
        let captureID = "dcap-" + String(repeating: "a", count: 64)
        let transport = Transport(.admitted(captureID: captureID))

        let outcomes = await CaptureDeliveryCoordinator(queue: queue, transport: transport).deliverPending()

        XCTAssertEqual(outcomes.map(\.state), [.admitted])
        XCTAssertEqual(outcomes[0].admittedCaptureID, captureID)
        XCTAssertEqual(transport.deliveredOperationKeys(), [entry.attempts[0].operationKey])
        XCTAssertEqual(outcomes[0].attempts.map(\.operationKey), [entry.attempts[0].operationKey])
        XCTAssertEqual(outcomes[0].attemptGeneration, entry.attemptGeneration)
    }

    func testRefusedReplayLeavesTheEntryUnknownWithItsKeyAndBytes() async throws {
        let (queue, entry, _) = try await unknownEntry()
        let transport = Transport(.definiteFailure(code: "invalid_request"))

        let outcomes = await CaptureDeliveryCoordinator(queue: queue, transport: transport).deliverPending()

        XCTAssertTrue(outcomes.isEmpty)
        XCTAssertEqual(transport.deliveredOperationKeys(), [entry.attempts[0].operationKey])
        assertUnchanged(queue, entry)
    }

    func testUnknownReplayLeavesTheEntryUnknownAndNeverAllocatesAKey() async throws {
        let (queue, entry, _) = try await unknownEntry()
        let transport = Transport(.unknown)
        let delivery = CaptureDeliveryCoordinator(queue: queue, transport: transport)

        for _ in 0..<3 { _ = await delivery.deliverPending() }

        XCTAssertEqual(transport.deliveredOperationKeys(), Array(repeating: entry.attempts[0].operationKey, count: 3))
        assertUnchanged(queue, entry)
    }

    func testReplayIsRefusedWhenTheDescriptorDiffers() async throws {
        let (queue, entry, _) = try await unknownEntry()
        let transport = Transport(
            .admitted(captureID: "dcap-" + String(repeating: "a", count: 64)),
            descriptorDigest: "sha256:" + String(repeating: "c", count: 64)
        )

        let outcomes = await CaptureDeliveryCoordinator(queue: queue, transport: transport).deliverPending()

        XCTAssertTrue(outcomes.isEmpty)
        XCTAssertTrue(transport.delivered().isEmpty)
        assertUnchanged(queue, entry)
    }

    func testReplayIsRefusedWhenTheBindingDiffers() async throws {
        let (queue, entry, _) = try await unknownEntry()
        let transport = Transport(
            .admitted(captureID: "dcap-" + String(repeating: "a", count: 64)),
            binding: .companion(workspaceRef: "workspace-2")
        )

        let outcomes = await CaptureDeliveryCoordinator(queue: queue, transport: transport).deliverPending()

        XCTAssertTrue(outcomes.isEmpty)
        XCTAssertTrue(transport.delivered().isEmpty)
        assertUnchanged(queue, entry)
    }

    func testReplayIsRefusedWhenThePayloadBytesChanged() async throws {
        let (queue, entry, payload) = try await unknownEntry()
        try Data("HELLO".utf8).write(to: payload)
        let transport = Transport(.admitted(captureID: "dcap-" + String(repeating: "a", count: 64)))

        let outcomes = await CaptureDeliveryCoordinator(queue: queue, transport: transport).deliverPending()

        XCTAssertTrue(outcomes.isEmpty)
        XCTAssertTrue(transport.delivered().isEmpty)
        XCTAssertEqual(queue.allEntries(), [entry])
    }

    private func makeIntent(
        payload: Data,
        binding: CaptureDeliveryBinding = .companion(workspaceRef: "workspace-1")
    ) throws -> CaptureIntentV3 {
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

    /// An intent frozen against `queued` must not be delivered by a transport
    /// trusted only for `trusted`: no transport call, no stage transition, and
    /// the item stays pending for recovery or reconfiguration.
    private func assertRefusesMismatch(
        queued: CaptureDeliveryBinding,
        trusted: CaptureDeliveryBinding = .companion(workspaceRef: "workspace-1"),
        line: UInt = #line
    ) async throws {
        let root = try temporaryDirectory()
        let queue = try CaptureIntentQueue(root: root)
        let payload = Data("hello".utf8)
        let intent = try makeIntent(payload: payload, binding: queued)
        try queue.enqueue(intent: intent, payload: payload)
        let transport = Transport(
            .admitted(captureID: "dcap-" + String(repeating: "a", count: 64)),
            binding: trusted
        )
        let delivery = CaptureDeliveryCoordinator(queue: queue, transport: transport)

        let outcomes = await delivery.deliverPending()

        XCTAssertTrue(outcomes.isEmpty, line: line)
        XCTAssertTrue(transport.delivered().isEmpty, line: line)
        let entry = try XCTUnwrap(queue.allEntries().first, line: line)
        XCTAssertEqual(entry.state, .pending, line: line)
        XCTAssertEqual(entry.attemptGeneration, 0, line: line)
        XCTAssertNil(entry.stageRequestDigest, line: line)
        XCTAssertNil(entry.stageDescriptorDigest, line: line)
        XCTAssertNoThrow(try queue.payloadURL(for: intent.itemId), line: line)
    }

    func testMismatchedWorkspaceIsNotDelivered() async throws {
        try await assertRefusesMismatch(queued: .companion(workspaceRef: "workspace-2"))
    }

    func testMismatchedTargetBindingRevisionIsNotDelivered() async throws {
        let trusted = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")
        try await assertRefusesMismatch(
            queued: CaptureDeliveryBinding(
                targetRef: trusted.targetRef,
                workspaceRef: trusted.workspaceRef,
                targetBindingRevision: "2",
                callerContextBinding: trusted.callerContextBinding
            )
        )
    }

    func testMismatchedCallerContextIsNotDelivered() async throws {
        let trusted = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")
        try await assertRefusesMismatch(
            queued: CaptureDeliveryBinding(
                targetRef: trusted.targetRef,
                workspaceRef: trusted.workspaceRef,
                targetBindingRevision: trusted.targetBindingRevision,
                callerContextBinding: CallerContextBinding(
                    clientContextRef: "macos-companion",
                    principalRef: "native-user",
                    contextRevision: "1"
                )
            )
        )
    }

    func testMismatchedTargetIsNotDelivered() async throws {
        try await assertRefusesMismatch(
            queued: .companion(workspaceRef: "workspace-1", targetRef: "other")
        )
    }

    func testCompanionBindingNamesTheRuntimeLocalPrincipal() {
        let binding = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")
        XCTAssertEqual(binding.callerContextBinding.principalRef, "local-user")
    }

    /// `/tmp` is a symlink to `/private/tmp`; the digest is the CLI's own
    /// (`os.path.realpath`), computed by `resolve_local_target`.
    func testInstalledCompanionBindingMatchesCLITargetDerivation() {
        let binding = CaptureDeliveryBinding.installedCompanion(
            installationState: URL(fileURLWithPath: "/tmp/omnivia-install/state.json"),
            workspaceRef: "workspace-1"
        )
        XCTAssertEqual(
            binding.targetRef,
            "core-target-7f88d1499b4deb288db3ebc433ab7f68"
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
