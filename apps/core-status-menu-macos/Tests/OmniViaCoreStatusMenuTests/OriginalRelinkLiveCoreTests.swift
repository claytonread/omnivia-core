import Foundation
import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

/// AT-27 against a real Core: the real `omnivia` CLI, a live `omnivia-core-service`, the
/// real `document.original.relink` operation behind its authentication and idempotency, the
/// production Locate flow and the production companion stores and reconciler. Nothing hands
/// the reconciler an outcome and nothing is written into Core's storage; the only
/// test-specific pieces are the native picker and alert that `OriginalRelinkFlow` is given,
/// and a transport wrapper that discards Core's first answer after the CLI has returned it,
/// which is what a lost acknowledgement is.
///
/// Needs a Python that can import this repository's packages
/// (`OMNIVIA_CORE_TEST_PYTHON`, as in `UnknownOutcomeAcceptanceTests`).
@MainActor
final class OriginalRelinkLiveCoreTests: XCTestCase {
    private struct BookmarkCodec: SecurityScopedBookmarkCoding {
        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
        }
        func startAccessing(_ url: URL) -> Bool { true }
        func stopAccessing(_ url: URL) {}
    }

    private final class TestClock: @unchecked Sendable {
        private let lock = NSLock()
        private var value: Int64 = 0
        func advance(_ delta: Int64) { lock.withLock { value += delta } }
        var reader: @Sendable () -> Int64 { { [self] in self.lock.withLock { self.value } } }
    }

    /// Delegates to the real CLI, records what it returned, and tells the reconciler that
    /// the answer never arrived.
    private final class LostAcknowledgementTransport: OriginalRelinkTransport, @unchecked Sendable {
        let real: CLICaptureDelivery
        var deliveryBinding: CaptureDeliveryBinding { real.deliveryBinding }
        private let lock = NSLock()
        private var log: [(key: String, input: Data, outcome: OriginalRelinkOutcome)] = []

        init(_ real: CLICaptureDelivery) { self.real = real }

        var committed: [(key: String, input: Data, outcome: OriginalRelinkOutcome)] {
            lock.withLock { log }
        }

        func deliverRelink(input: Data, operationKey: String) async -> OriginalRelinkOutcome {
            let outcome = await real.deliverRelink(input: input, operationKey: operationKey)
            lock.withLock { log.append((operationKey, input, outcome)) }
            return .unknown
        }
    }

    private final class RecordingTransport: OriginalRelinkTransport, @unchecked Sendable {
        let real: CLICaptureDelivery
        var deliveryBinding: CaptureDeliveryBinding { real.deliveryBinding }
        private let lock = NSLock()
        private var log: [(key: String, input: Data, outcome: OriginalRelinkOutcome)] = []

        init(_ real: CLICaptureDelivery) { self.real = real }

        var sent: [(key: String, input: Data, outcome: OriginalRelinkOutcome)] {
            lock.withLock { log }
        }

        func deliverRelink(input: Data, operationKey: String) async -> OriginalRelinkOutcome {
            let outcome = await real.deliverRelink(input: input, operationKey: operationKey)
            lock.withLock { log.append((operationKey, input, outcome)) }
            return outcome
        }
    }

    func testALostRelinkAcknowledgementIsReconciledOnTheSameKeyAndTheOldGenerationStaysHistoryThroughTheRealCLI()
        async throws
    {
        let core = try LiveCore.initialise(in: self)
        try core.startService()
        let binding = core.delivery.deliveryBinding
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("note.txt")
        let chosen = root.appendingPathComponent("note-v2.txt")
        try Data("first".utf8).write(to: original)
        try Data("third".utf8).write(to: chosen)
        let stateRoot = root.appendingPathComponent("state", isDirectory: true)
        let clock = TestClock()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: stateRoot, bookmarkCodec: BookmarkCodec(), monotonicClock: clock.reader
        )

        // A watched file, added the production way and captured by the real service.
        _ = try await ManualCaptureSubmission(environment: environment, binding: binding)
            .submit([ManualCaptureCandidate(url: original, keepUpdated: true)])
        let baseline = await CaptureDeliveryCoordinator(
            queue: environment.queue, transport: core.delivery
        ).deliverPending()
        XCTAssertEqual(baseline.map(\.state), [.admitted])
        let headCapture = try XCTUnwrap(baseline.first?.admittedCaptureID)
        _ = await environment.fileMonitoring.reconcileReceipts()
        let watch = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(watch.state, .active)
        XCTAssertEqual(watch.bindingGeneration, 1)
        XCTAssertEqual(try core.bindingState(documentID: watch.linkedDocumentID), "connected")

        // A refresh of the old file is published, and still queued, when the person Locates.
        try Data("second".utf8).write(to: original)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [watch.subscriptionID])
        clock.advance(3_000)
        _ = await environment.fileMonitoring.reconcileAll()
        let queued = try XCTUnwrap(
            environment.queue.allEntries().first { $0.intent.surface == .automaticFile }
        )
        XCTAssertEqual(queued.intent.originalBinding?.bindingGeneration, 1)

        // Locate, through the production flow, compared against what Core really holds.
        var prompts: [OriginalRelinkPrompt] = []
        let flow = OriginalRelinkFlow(
            environment: environment,
            fallback: binding,
            chooseFile: { chosen },
            decide: { prompt in
                prompts.append(prompt)
                return .useThisFile
            },
            refuse: { XCTFail("unexpected refusal: \($0)") },
            addSeparately: { _ in XCTFail("not asked") },
            onReserved: {}
        )
        let history = try XCTUnwrap(core.currentHistory(documentID: watch.linkedDocumentID))
        let result = await flow.locate(watch, history: history)
        XCTAssertEqual(result, .reserved(accessRef: watch.accessRef, generation: 2))
        XCTAssertEqual(prompts.map(\.comparison), [.differs])
        let owed = try XCTUnwrap(environment.accessStore.pendingOriginalRelinks().first)
        XCTAssertEqual(owed.generation, 2)
        XCTAssertEqual(owed.binding, NativeRootAccessBinding(binding))
        XCTAssertEqual(environment.documentAccessRoutes().map(\.state), [.relinking])
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.paused])

        // Core commits; the first acknowledgement is lost.
        let lost = LostAcknowledgementTransport(core.delivery)
        let firstPass = await environment.originalRelinkReconciler(transport: lost).reconcilePending()
        XCTAssertEqual(firstPass, [.owed(operationKey: owed.operationKey)])
        let attempt = try XCTUnwrap(lost.committed.first)
        XCTAssertEqual(attempt.key, owed.operationKey)
        guard case let .acknowledged(documentID, ownerRef, generation, head) = attempt.outcome else {
            return XCTFail("Core did not commit the first attempt: \(attempt.outcome)")
        }
        XCTAssertEqual(documentID, watch.linkedDocumentID)
        XCTAssertEqual(ownerRef, watch.accessRef)
        XCTAssertEqual(generation, 2)
        XCTAssertEqual(head, headCapture, "Core reports the head it held when the relink committed")
        // Core's own statement: it committed before the answer was lost.
        XCTAssertEqual(try core.bindingState(documentID: watch.linkedDocumentID), "connected")
        XCTAssertEqual(environment.accessStore.pendingOriginalRelinks(), [owed])
        XCTAssertEqual(environment.documentAccessRoutes().map(\.state), [.relinking])

        // The companion restarts. The held route and the one intent survived; the selected
        // file has still not been read through any grant, and nothing was reissued.
        let restarted = try DocumentIntakeEnvironment(
            stateRoot: stateRoot, bookmarkCodec: BookmarkCodec(), monotonicClock: clock.reader
        )
        XCTAssertEqual(restarted.accessStore.pendingOriginalRelinks(), [owed])
        XCTAssertEqual(restarted.documentAccessRoutes().map(\.state), [.relinking])
        XCTAssertEqual(restarted.documentAccessRoutes().map(\.generation), [2])
        XCTAssertEqual(restarted.fileSubscriptions.all().map(\.bindingGeneration), [1])
        for expected in [UInt64(1), 2] {
            let refusal = XCTAssertThrowsErrorResult {
                try restarted.accessStore.withReadAccess(
                    accessRef: watch.accessRef, expectedGeneration: expected
                ) { _ in Data() }
            }
            XCTAssertEqual(refusal as? NativeAccessError, .relinkPending)
        }

        // The restarted reconciler resumes the same canonical mutation under the same key.
        let real = RecordingTransport(core.delivery)
        let secondPass = await restarted.originalRelinkReconciler(transport: real).reconcilePending()
        XCTAssertEqual(secondPass, [.acknowledged(operationKey: owed.operationKey)])
        let resumed = try XCTUnwrap(real.sent.first)
        XCTAssertEqual(real.sent.count, 1)
        XCTAssertEqual(resumed.key, attempt.key)
        XCTAssertEqual(resumed.input, attempt.input)
        XCTAssertEqual(resumed.outcome, attempt.outcome)
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: attempt.input) as? [String: Any])
        XCTAssertEqual(
            body as NSDictionary,
            [
                "document_id": watch.linkedDocumentID,
                "owner_ref": watch.accessRef,
                "binding_generation": 2,
            ] as NSDictionary
        )

        // Only that answer activated the new grant, the watch moved with it, and automatic
        // updates are still paused until the person resumes them.
        XCTAssertEqual(restarted.accessStore.pendingOriginalRelinks(), [])
        let route = try XCTUnwrap(restarted.documentAccessRoutes().first)
        XCTAssertEqual(route.state, .connected)
        XCTAssertEqual(route.generation, 2)
        let moved = try XCTUnwrap(restarted.fileSubscriptions.all().first)
        XCTAssertEqual(moved.bindingGeneration, 2)
        XCTAssertEqual(moved.state, .paused)
        XCTAssertEqual(moved.currentSuccessfulCaptureID, headCapture)
        let url = try restarted.accessStore.withReadAccess(accessRef: watch.accessRef, expectedGeneration: 2) { $0 }
        XCTAssertEqual(url.lastPathComponent, "note-v2.txt")
        let stale = XCTAssertThrowsErrorResult {
            try restarted.accessStore.withReadAccess(accessRef: watch.accessRef, expectedGeneration: 1) { _ in Data() }
        }
        XCTAssertEqual(stale as? NativeAccessError, .generationMismatch)
        let quiet = await restarted.originalRelinkReconciler(transport: real).reconcilePending()
        XCTAssertEqual(quiet, [])

        // Core's answer to the key is one stored answer: replaying it as many times as asked
        // returns the identical result and acts on nothing.
        let replayA = try core.runRelink(input: attempt.input, operationKey: owed.operationKey)
        let replayB = try core.runRelink(input: attempt.input, operationKey: owed.operationKey)
        XCTAssertEqual(replayA["result"] as? NSDictionary, replayB["result"] as? NSDictionary)
        let auditA = (replayA["metadata"] as? [String: Any])?["audit_reference"] as? String
        XCTAssertNotNil(auditA)
        XCTAssertEqual(auditA, (replayB["metadata"] as? [String: Any])?["audit_reference"] as? String)
        let replayed = try XCTUnwrap(replayA["result"] as? [String: Any])
        XCTAssertEqual(replayed["connection_state"] as? String, "connected")
        XCTAssertEqual(replayed["binding_generation"] as? Int, 2)
        XCTAssertEqual(replayed["current_capture_id"] as? String, headCapture)

        // The refresh published before the Locate is still delivered, and Core keeps it as
        // history: preserved, never current, never the base of what follows.
        let late = await CaptureDeliveryCoordinator(
            queue: restarted.queue, transport: core.delivery
        ).deliverPending()
        XCTAssertEqual(late.map(\.state), [.admitted])
        _ = await restarted.fileMonitoring.reconcileReceipts()
        let afterLate = try XCTUnwrap(restarted.fileSubscriptions.all().first)
        XCTAssertEqual(afterLate.currentSuccessfulCaptureID, headCapture)
        let afterHistory = try core.captureDispositions(documentID: watch.linkedDocumentID)
        XCTAssertEqual(afterHistory.map(\.disposition), ["head_eligible", "historical_only"])
        XCTAssertEqual(afterHistory.map(\.reason), ["eligible", "superseded_generation"])
        XCTAssertEqual(afterHistory.map(\.current), [true, false])

        // The explicit Resume reads the selected file, under the new generation, for the
        // same document; Core takes it as the new head.
        _ = try await restarted.fileMonitoring.resume(subscriptionID: watch.subscriptionID)
        clock.advance(3_000)
        _ = await restarted.fileMonitoring.reconcileAll()
        let published = try XCTUnwrap(
            restarted.queue.allEntries().first {
                $0.intent.surface == .automaticFile && $0.intent.itemId != queued.intent.itemId
            }
        )
        XCTAssertEqual(published.intent.originalBinding?.bindingGeneration, 2)
        XCTAssertEqual(published.intent.originalBinding?.linkedDocumentId, watch.linkedDocumentID)
        let newest = await CaptureDeliveryCoordinator(
            queue: restarted.queue, transport: core.delivery
        ).deliverPending()
        XCTAssertEqual(newest.map(\.state), [.admitted])
        let finalHistory = try core.captureDispositions(documentID: watch.linkedDocumentID)
        XCTAssertEqual(finalHistory.map(\.disposition), ["head_eligible", "historical_only", "head_eligible"])
        XCTAssertEqual(finalHistory.map(\.association), ["active_binding", "historical_binding", "active_binding"])
        XCTAssertEqual(finalHistory.map(\.current), [false, false, true])

        // Core holds exactly one binding for the owner, connected at the reserved
        // generation, and exactly one committed relink.
        core.stopService()
        let stored = try core.storedBindingRecords(operation: "document.original.relink")
        XCTAssertEqual(stored.bindings, [[watch.accessRef, watch.linkedDocumentID, "connected", "2"]])
        XCTAssertEqual(stored.claims, 1)
    }

    // MARK: - Fixtures

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
            .appendingPathComponent("omnivia-original-relink-live-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
