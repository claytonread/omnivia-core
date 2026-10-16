import CryptoKit
import Foundation
import XCTest
@testable import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

/// AT-30 against a real Core: the real `omnivia` CLI, a live `omnivia-core-service`, the
/// real `document.original.disconnect` operation behind its authentication and idempotency,
/// and the production companion stores and reconciler. Nothing hands the reconciler an
/// outcome and nothing is written into Core's storage; the only test-specific piece is a
/// transport wrapper that discards Core's first answer after the CLI has returned it, which
/// is what a lost acknowledgement is.
///
/// Needs a Python that can import this repository's packages
/// (`OMNIVIA_CORE_TEST_PYTHON`, as in `UnknownOutcomeAcceptanceTests`).
@MainActor
final class OriginalDisconnectLiveCoreTests: XCTestCase {
    private struct BookmarkCodec: SecurityScopedBookmarkCoding {
        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
        }
        func startAccessing(_ url: URL) -> Bool { true }
        func stopAccessing(_ url: URL) {}
    }

    /// Delegates to the real CLI, records what it returned, and tells the reconciler that
    /// the answer never arrived.
    private final class LostAcknowledgementTransport: OriginalDisconnectTransport, @unchecked Sendable {
        let real: CLICaptureDelivery
        var deliveryBinding: CaptureDeliveryBinding { real.deliveryBinding }
        private let lock = NSLock()
        private var log: [(key: String, input: Data, outcome: OriginalDisconnectOutcome)] = []

        init(_ real: CLICaptureDelivery) { self.real = real }

        var committed: [(key: String, input: Data, outcome: OriginalDisconnectOutcome)] {
            lock.withLock { log }
        }

        func deliver(input: Data, operationKey: String) async -> OriginalDisconnectOutcome {
            let outcome = await real.deliver(input: input, operationKey: operationKey)
            lock.withLock { log.append((operationKey, input, outcome)) }
            return .unknown
        }
    }

    private final class RecordingTransport: OriginalDisconnectTransport, @unchecked Sendable {
        let real: CLICaptureDelivery
        var deliveryBinding: CaptureDeliveryBinding { real.deliveryBinding }
        private let lock = NSLock()
        private var log: [(key: String, input: Data, outcome: OriginalDisconnectOutcome)] = []

        init(_ real: CLICaptureDelivery) { self.real = real }

        var sent: [(key: String, input: Data, outcome: OriginalDisconnectOutcome)] {
            lock.withLock { log }
        }

        func deliver(input: Data, operationKey: String) async -> OriginalDisconnectOutcome {
            let outcome = await real.deliver(input: input, operationKey: operationKey)
            lock.withLock { log.append((operationKey, input, outcome)) }
            return outcome
        }
    }

    func testALostDisconnectAcknowledgementAfterCoreCommitsIsReconciledOnTheSameKeyThroughTheRealCLI()
        async throws
    {
        let core = try LiveCore.initialise(in: self)
        try core.startService()
        let binding = core.delivery.deliveryBinding
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("note.txt")
        try Data("first".utf8).write(to: original)
        let stateRoot = root.appendingPathComponent("state", isDirectory: true)
        let environment = try DocumentIntakeEnvironment(stateRoot: stateRoot, bookmarkCodec: BookmarkCodec())

        // A linked file: a local grant, and a real capture that makes Core record its binding.
        let grant = try environment.accessStore.adopt(
            accessRef: "access-1",
            linkedDocumentID: "doc-1",
            selectedURL: original,
            route: .directFile
        )
        XCTAssertEqual(grant.generation, 1)
        let first = Data("first".utf8)
        try environment.queue.enqueue(
            intent: try intent(item: 1, payload: first, binding: binding, base: nil),
            payload: first
        )
        let captured = await CaptureDeliveryCoordinator(
            queue: environment.queue,
            transport: core.delivery
        ).deliverPending()
        XCTAssertEqual(captured.map(\.state), [.admitted])
        let headCapture = try XCTUnwrap(captured.first?.admittedCaptureID)
        XCTAssertEqual(try core.bindingState(documentID: "doc-1"), "connected")

        // A refresh was already queued before the person disconnects.
        let second = Data("second".utf8)
        try environment.queue.enqueue(
            intent: try intent(item: 2, payload: second, binding: binding, base: headCapture),
            payload: second
        )

        // Disconnect: local revocation first and at once, intent in the same write.
        let intents = try await environment.disconnectSource(
            linkedDocumentID: "doc-1",
            fallback: binding
        )
        let owed = try XCTUnwrap(intents.first)
        XCTAssertEqual(intents.count, 1)
        XCTAssertEqual(owed.accessRef, "access-1")
        XCTAssertEqual(owed.binding, NativeRootAccessBinding(binding))
        XCTAssertEqual(environment.documentAccessRoutes().map(\.state), [.disconnected])

        // Core commits; the first acknowledgement is lost.
        let lost = LostAcknowledgementTransport(core.delivery)
        let firstPass = await environment.originalDisconnectReconciler(transport: lost).reconcilePending()
        XCTAssertEqual(firstPass, [.owed(operationKey: owed.operationKey)])
        let attempt = try XCTUnwrap(lost.committed.first)
        XCTAssertEqual(attempt.key, owed.operationKey)
        guard case .acknowledged("doc-1", "access-1") = attempt.outcome else {
            return XCTFail("Core did not commit the first attempt: \(attempt.outcome)")
        }
        // Core's own statement, not the companion's: it committed before the answer was lost.
        XCTAssertEqual(try core.bindingState(documentID: "doc-1"), "disconnected")
        XCTAssertEqual(environment.accessStore.pendingOriginalDisconnects(), [owed])

        // The companion restarts. The revocation and the one intent survived; nothing was
        // reissued.
        let restarted = try DocumentIntakeEnvironment(stateRoot: stateRoot, bookmarkCodec: BookmarkCodec())
        XCTAssertEqual(restarted.documentAccessRoutes().map(\.state), [.disconnected])
        XCTAssertEqual(restarted.documentAccessRoutes().map(\.generation), [1])
        XCTAssertEqual(restarted.accessStore.pendingOriginalDisconnects(), [owed])
        let refusal = XCTAssertThrowsErrorResult {
            try restarted.accessStore.withReadAccess(accessRef: "access-1", expectedGeneration: 1) { _ in Data() }
        }
        XCTAssertEqual(refusal as? NativeAccessError, .disconnected)
        _ = try await restarted.disconnectSource(linkedDocumentID: "doc-1", fallback: binding)
        XCTAssertEqual(restarted.accessStore.pendingOriginalDisconnects(), [owed])

        // The restarted reconciler resumes the same canonical mutation under the same key.
        let real = RecordingTransport(core.delivery)
        let secondPass = await restarted.originalDisconnectReconciler(transport: real).reconcilePending()
        XCTAssertEqual(secondPass, [.acknowledged(operationKey: owed.operationKey)])
        let resumed = try XCTUnwrap(real.sent.first)
        XCTAssertEqual(real.sent.count, 1)
        XCTAssertEqual(resumed.key, attempt.key)
        XCTAssertEqual(resumed.input, attempt.input)
        XCTAssertEqual(resumed.outcome, .acknowledged(documentID: "doc-1", ownerRef: "access-1"))
        XCTAssertEqual(restarted.accessStore.pendingOriginalDisconnects(), [])
        let quiet = await restarted.originalDisconnectReconciler(transport: real).reconcilePending()
        XCTAssertEqual(quiet, [])

        // Core's answer to the key is one stored answer: replaying it again, as many times
        // as asked, returns the identical result and acts on nothing.
        let replayA = try core.runDisconnect(input: attempt.input, operationKey: owed.operationKey)
        let replayB = try core.runDisconnect(input: attempt.input, operationKey: owed.operationKey)
        // The CLI mints a fresh request id per run; the stored result and the audit record
        // of the one commit are what a replay must repeat.
        XCTAssertEqual(replayA["result"] as? NSDictionary, replayB["result"] as? NSDictionary)
        let auditA = (replayA["metadata"] as? [String: Any])?["audit_reference"] as? String
        XCTAssertNotNil(auditA)
        XCTAssertEqual(auditA, (replayB["metadata"] as? [String: Any])?["audit_reference"] as? String)
        let result = try XCTUnwrap(replayA["result"] as? [String: Any])
        XCTAssertEqual(result["connection_state"] as? String, "disconnected")
        XCTAssertEqual(result["document_id"] as? String, "doc-1")
        XCTAssertEqual(result["owner_ref"] as? String, "access-1")
        XCTAssertEqual(result["binding_generation"] as? Int, 1)

        // The refresh that was queued before the disconnect is still delivered, and Core
        // takes it as history: it does not recreate an active binding.
        let late = await CaptureDeliveryCoordinator(
            queue: restarted.queue,
            transport: core.delivery
        ).deliverPending()
        XCTAssertEqual(late.map(\.state), [.admitted])
        XCTAssertEqual(try core.bindingState(documentID: "doc-1"), "disconnected")
        XCTAssertEqual(restarted.documentAccessRoutes().map(\.state), [.disconnected])
        XCTAssertEqual(restarted.accessStore.pendingOriginalDisconnects(), [])

        // Core holds exactly one binding for the owner, disconnected at the generation it
        // was captured under, and exactly one committed disconnect.
        core.stopService()
        let stored = try core.storedDisconnectRecords()
        XCTAssertEqual(stored.bindings, [["access-1", "doc-1", "disconnected", "1"]])
        XCTAssertEqual(stored.claims, 1)
    }

    // MARK: - Fixtures

    private func intent(
        item: Int,
        payload: Data,
        binding: CaptureDeliveryBinding,
        base: String?
    ) throws -> CaptureIntentV3 {
        let digest = "sha256:" + SHA256.hash(data: payload).map { String(format: "%02x", $0) }.joined()
        return try CaptureIntentV3(
            submissionId: "submission-\(item)",
            itemId: "item-\(item)",
            targetRef: binding.targetRef,
            workspaceRef: binding.workspaceRef,
            targetBindingRevision: binding.targetBindingRevision,
            callerContextBinding: binding.callerContextBinding,
            surface: .manual,
            contentKind: .text,
            payloadRef: "payload-\(item)",
            byteLength: UInt64(payload.count),
            localIntegrityDigest: digest,
            requestedSourceIdentity: "source-1",
            operationKey: "capture-\(item)",
            intentKind: base == nil ? .newCapture : .refresh,
            basePrecondition: base == nil ? .create : .expected(base),
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

    private func XCTAssertThrowsErrorResult<T>(
        file: StaticString = #filePath,
        line: UInt = #line,
        _ body: () throws -> T
    ) -> Error? {
        do {
            _ = try body()
            XCTFail("expected a failure", file: file, line: line)
            return nil
        } catch {
            return error
        }
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-original-disconnect-live-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
