import Foundation
import XCTest
@testable import DocumentIntakeMacOS

/// Two acceptance gates about what survives when an exchange with Core does not
/// finish, each against the production path the gate names.
///
/// * AT-30 (the capture half) an original disconnected while a capture for
///   it is in flight, whose acknowledgement is lost, keeps the local revocation,
///   and the reconciliation that resumes is the same canonical key. The lost
///   *disconnect* acknowledgement is `OriginalDisconnectAcceptanceTests` and
///   `OriginalDisconnectLiveCoreTests`.
/// * AT-32 a refresh that fails leaves the prior successful capture in place and
///   is itself a separate, retrievable outcome.
final class CaptureRecoveryAcceptanceTests: XCTestCase {
    private struct BookmarkCodec: SecurityScopedBookmarkCoding {
        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
        }
        func startAccessing(_ url: URL) -> Bool { true }
        func stopAccessing(_ url: URL) {}
    }

    /// The process-monotonic clock the scheduler measures its quiet period in.
    private final class TestClock: @unchecked Sendable {
        private let lock = NSLock()
        private var value: Int64 = 0

        func advance(_ delta: Int64) { lock.withLock { value += delta } }
        var reader: @Sendable () -> Int64 { { [self] in self.lock.withLock { self.value } } }
    }

    /// One attempt against Core, recording exactly what went over the wire so
    /// the key a later attempt reconciles on can be compared with it.
    private final class RecordingTransport: CaptureDeliveryTransport, @unchecked Sendable {
        let descriptorDigest = "sha256:" + String(repeating: "c", count: 64)
        let deliveryBinding = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")

        private let outcome: CaptureDeliveryOutcome
        private let lock = NSLock()
        private var inputs: [Data] = []

        init(_ outcome: CaptureDeliveryOutcome) { self.outcome = outcome }

        var sent: [[String: Any]] {
            lock.withLock { inputs }.compactMap {
                (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any]
            }
        }

        func deliver(
            input: Data,
            payloadURL: URL,
            operationKey: String
        ) async -> CaptureDeliveryOutcome {
            lock.withLock { inputs.append(input) }
            return outcome
        }
    }

    // MARK: - AT-30 (capture half) · a lost capture acknowledgement keeps the revocation

    /// The person disconnects an original while a capture for it is still in
    /// flight, and the answer to that attempt never comes back. The revocation
    /// is local and durable, so it survives the restart whole — and the work
    /// that resumes is reconciled under the same owner and document the lost
    /// attempt named, not a replacement identity.
    func testALostAcknowledgementKeepsTheDisconnectAndResumesOnTheSameKey() async throws {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("watched.txt")
        try Data("baseline".utf8).write(to: original)
        let stateRoot = root.appendingPathComponent("state", isDirectory: true)
        let clock = TestClock()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: stateRoot,
            bookmarkCodec: BookmarkCodec(),
            monotonicClock: clock.reader
        )
        let watch = try await activeWatch(environment, original: original)

        // One capture is published and then handed to Core.
        try Data("changed".utf8).write(to: original)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [watch.subscriptionID])
        clock.advance(3_000)
        _ = await environment.fileMonitoring.reconcileAll()
        let published = try XCTUnwrap(
            environment.queue.allEntries().first { $0.intent.surface == .automaticFile }
        )

        // The person disconnects the original while that attempt is open, and
        // the attempt's answer is lost.
        let revoked = try environment.accessStore.disconnect(
            linkedDocumentID: watch.linkedDocumentID
        )
        XCTAssertEqual(revoked.map(\.accessRef), [watch.accessRef])
        let lost = RecordingTransport(.unknown)
        let attempt = await CaptureDeliveryCoordinator(
            queue: environment.queue,
            transport: lost
        ).deliverPending()
        XCTAssertEqual(attempt.map(\.state), [.unknownOutcome])

        // A restart over the same durable state.
        let restarted = try DocumentIntakeEnvironment(
            stateRoot: stateRoot,
            bookmarkCodec: BookmarkCodec(),
            monotonicClock: clock.reader
        )

        // The revocation survived it, and nothing about the grant was reissued.
        XCTAssertEqual(
            restarted.accessStore.descriptors().map { "\($0.accessRef)=\($0.state)" },
            ["\(watch.accessRef)=disconnected"]
        )
        let refusal = try failure {
            try restarted.accessStore.withReadAccess(
                accessRef: watch.accessRef,
                expectedGeneration: watch.bindingGeneration
            ) { _ in Data() }
        }
        XCTAssertEqual(refusal as? NativeAccessError, .disconnected)

        // The work the lost attempt was for is still exactly one item, under the
        // identity it was published with.
        let recovered = try XCTUnwrap(
            restarted.queue.allEntries().first { $0.intent.itemId == published.intent.itemId }
        )
        XCTAssertEqual(recovered.state, .unknownOutcome)
        XCTAssertEqual(recovered.intent.operationKey, published.intent.operationKey)
        XCTAssertEqual(
            recovered.intent.originalBinding?.accessRecordId,
            published.intent.originalBinding?.accessRecordId
        )
        XCTAssertEqual(
            recovered.intent.originalBinding?.linkedDocumentId,
            published.intent.originalBinding?.linkedDocumentId
        )
        // A revoked source is never read again, so no second intent appears for
        // it however often reconciliation runs.
        _ = await restarted.fileMonitoring.reconcileAll()
        XCTAssertEqual(
            restarted.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            1
        )

        // Resuming it is the same canonical reconciliation: the attempt whose
        // effect is now known to be nothing returns to pending, and what goes
        // over the wire names the same owner and the same document.
        let open = try XCTUnwrap(recovered.attempts.last)
        _ = try restarted.queue.recoverKnownNoEffect(
            itemID: recovered.intent.itemId,
            attemptGeneration: recovered.attemptGeneration,
            requestDigest: try XCTUnwrap(recovered.stageRequestDigest),
            descriptorDigest: try XCTUnwrap(recovered.stageDescriptorDigest)
        )
        let resumed = RecordingTransport(.admitted(captureID: captureID("b")))
        let settled = await CaptureDeliveryCoordinator(
            queue: restarted.queue,
            transport: resumed
        ).deliverPending()
        XCTAssertEqual(settled.map(\.state), [.admitted])

        let first = try XCTUnwrap(lost.sent.first)
        let second = try XCTUnwrap(resumed.sent.first)
        XCTAssertEqual(second["document_id"] as? String, first["document_id"] as? String)
        XCTAssertEqual(
            (second["original_binding"] as? [String: Any])?["owner_ref"] as? String,
            (first["original_binding"] as? [String: Any])?["owner_ref"] as? String
        )
        XCTAssertEqual(
            second["requested_source_identity"] as? String,
            first["requested_source_identity"] as? String
        )
        // Only the per-attempt key advances, which is what keeps a replacement
        // attempt from reusing the key of one whose effect was unknown.
        XCTAssertNotEqual(
            try XCTUnwrap(restarted.queue.allEntries().first?.attempts.last).operationKey,
            open.operationKey
        )
    }

    // MARK: - AT-32 · a failed refresh is its own outcome

    /// A refresh that Core refuses. The capture this watch already has is still
    /// the one it holds, and the failure is a separate record that survives on
    /// its own — the two are retrievable side by side, across a restart.
    func testAFailedRefreshKeepsThePriorCaptureAndIsItsOwnRetrievableOutcome() async throws {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("watched.txt")
        try Data("baseline".utf8).write(to: original)
        let stateRoot = root.appendingPathComponent("state", isDirectory: true)
        let clock = TestClock()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: stateRoot,
            bookmarkCodec: BookmarkCodec(),
            monotonicClock: clock.reader
        )
        let watch = try await activeWatch(environment, original: original)
        let head = captureID("a")
        XCTAssertEqual(
            environment.fileSubscriptions.all().first?.currentSuccessfulCaptureID,
            head
        )

        // The refresh publishes, and Core refuses it outright.
        try Data("changed".utf8).write(to: original)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [watch.subscriptionID])
        clock.advance(3_000)
        _ = await environment.fileMonitoring.reconcileAll()
        // Activity is recorded after publication and again after the delivery
        // pass, which is the cadence the companion keeps.
        try environment.recordCaptureActivity()
        let refused = await CaptureDeliveryCoordinator(
            queue: environment.queue,
            transport: RecordingTransport(.definiteFailure(code: "unsupported_media_type"))
        ).deliverPending()
        XCTAssertEqual(refused.map(\.state), [.failed])
        try environment.recordCaptureActivity()
        _ = await environment.fileMonitoring.reconcileAll()

        // The capture that succeeded is untouched: the failure replaced nothing.
        let after = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(after.currentSuccessfulCaptureID, head)
        XCTAssertEqual(after.state, .paused)
        // The evidence of the refused version is cleared so it is read again,
        // which is not the same as losing what was already captured.
        XCTAssertNil(after.lastObservedDigest)

        // The failure is its own retrievable outcome beside that capture, with
        // the reason it carries.
        let failed = try XCTUnwrap(
            environment.queue.allEntries().first { $0.intent.surface == .automaticFile }
        )
        XCTAssertEqual(failed.state, .failed)
        XCTAssertEqual(failed.failureCode, "unsupported_media_type")
        XCTAssertNotEqual(failed.intent.itemId, watch.baselineItemID)
        let records = environment.captureActivity.all()
        XCTAssertEqual(records.first { $0.itemID == failed.intent.itemId }?.fact, .failed)
        // Beside it, and not instead of it: the capture that succeeded is its
        // own record and keeps saying so.
        XCTAssertNotEqual(
            records.first { $0.itemID == watch.baselineItemID }?.fact,
            .failed
        )

        // Both survive a restart, still as two separate facts.
        let restarted = try DocumentIntakeEnvironment(
            stateRoot: stateRoot,
            bookmarkCodec: BookmarkCodec(),
            monotonicClock: clock.reader
        )
        XCTAssertEqual(
            restarted.fileSubscriptions.all().first?.currentSuccessfulCaptureID,
            head
        )
        XCTAssertEqual(
            restarted.queue.allEntries().first { $0.intent.itemId == failed.intent.itemId }?.state,
            .failed
        )
        XCTAssertEqual(
            restarted.captureActivity.record(itemID: failed.intent.itemId)?.fact,
            .failed
        )
    }

    // MARK: - Fixtures

    private struct Watch {
        let subscriptionID: String
        let accessRef: String
        let linkedDocumentID: String
        let bindingGeneration: UInt64
        let baselineItemID: String
    }

    /// One admitted baseline watch over `original`, as the manual Add that
    /// established it leaves things.
    private func activeWatch(
        _ environment: DocumentIntakeEnvironment,
        original: URL
    ) async throws -> Watch {
        let baseline = try await ManualCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        ).submit([ManualCaptureCandidate(url: original, keepUpdated: true)])
        let digest = "sha256:" + String(repeating: "d", count: 64)
        let staging = try environment.queue.beginStage(
            itemID: baseline[0].intent.itemId,
            requestDigest: digest,
            descriptorDigest: digest
        )
        _ = try environment.queue.markAdmitted(
            itemID: baseline[0].intent.itemId,
            attemptGeneration: staging.attemptGeneration,
            captureID: captureID("a")
        )
        _ = await environment.fileMonitoring.reconcileReceipts()
        let value = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(value.state, .active)
        return Watch(
            subscriptionID: value.subscriptionID,
            accessRef: value.accessRef,
            linkedDocumentID: value.linkedDocumentID,
            bindingGeneration: value.bindingGeneration,
            baselineItemID: value.baselineItemID
        )
    }

    private func captureID(_ fill: String) -> String {
        "dcap-" + String(repeating: fill, count: 64)
    }

    /// Fails the test unless `body` throws, and hands back what it threw.
    private func failure<T>(
        file: StaticString = #filePath,
        line: UInt = #line,
        _ body: () throws -> T
    ) throws -> Error {
        do {
            _ = try body()
            XCTFail("expected a failure", file: file, line: line)
            throw CaptureDeliveryError.invalidIntent
        } catch {
            return error
        }
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-capture-recovery-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
