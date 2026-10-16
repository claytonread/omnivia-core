import Foundation
import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS

/// AT-32 and AT-33 against a real Core: the real `omnivia` CLI, a live `omnivia-core-service`,
/// the real `document.capture` and `document.history` operations, the production companion
/// stores and coordinator, the production verifier and the production Files window model.
/// Nothing is written into Core's storage; the retention decision is made through the
/// service's own `expire_capture` with the service stopped.
///
/// Needs a Python that can import this repository's packages (`OMNIVIA_CORE_TEST_PYTHON`, as
/// in `UnknownOutcomeAcceptanceTests`).
@MainActor
final class RefreshOutcomeLiveCoreTests: XCTestCase {
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

    private struct Rig {
        let core: LiveCore
        let environment: DocumentIntakeEnvironment
        let clock: TestClock
        let original: URL
        let watch: FileSubscription

        func check() async -> [FileMonitoringReport] {
            await environment.fileMonitoring.markDirty(subscriptionIDs: [watch.subscriptionID])
            clock.advance(3_000)
            return await environment.fileMonitoring.reconcileAll()
        }

        var subscription: FileSubscription { environment.fileSubscriptions.all()[0] }
    }

    /// A watched file, added the production way and captured by the real service, with its
    /// searchable version published.
    private func rig(content: Data, file: String = "note.txt") async throws -> Rig {
        let core = try LiveCore.initialise(in: self)
        try core.startService()
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-refresh-live-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let original = root.appendingPathComponent(file)
        try content.write(to: original)
        let clock = TestClock()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec(),
            monotonicClock: clock.reader,
            currentCapture: CoreHistoryCurrentCaptureVerifier(transport: core.historyTransport)
        )
        _ = try await ManualCaptureSubmission(environment: environment, binding: core.delivery.deliveryBinding)
            .submit([ManualCaptureCandidate(url: original, keepUpdated: true)])
        let baseline = await CaptureDeliveryCoordinator(
            queue: environment.queue, transport: core.delivery
        ).deliverPending()
        XCTAssertEqual(baseline.map(\.state), [.admitted])
        _ = await environment.fileMonitoring.reconcileReceipts()
        let watch = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(watch.state, .active)
        let rig = Rig(core: core, environment: environment, clock: clock, original: original, watch: watch)
        _ = try await settled(core, documentID: watch.linkedDocumentID, extractions: 1)
        return rig
    }

    /// Core's history once it holds `extractions` extraction revisions that all have a result.
    private func settled(
        _ core: LiveCore,
        documentID: String,
        extractions: Int
    ) async throws -> CoreDocumentHistory {
        for _ in 0..<150 {
            if let history = core.currentHistory(documentID: documentID),
               history.extractions.count == extractions,
               history.extractions.allSatisfy({ !$0.isPending })
            {
                return history
            }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        XCTFail("Core did not settle \(extractions) extraction(s) for \(documentID)")
        throw XCTSkip("Core did not process the capture")
    }

    func testSameBytesWhoseCaptureCoreNoLongerRetainsAreNeverCalledUpToDateThroughTheRealCore()
        async throws
    {
        let rig = try await rig(content: Data("baseline".utf8))
        let documentID = rig.watch.linkedDocumentID
        let head = try XCTUnwrap(rig.watch.currentSuccessfulCaptureID)

        // Core stands behind the bytes: the unchanged file is checked, on Core's own statement.
        _ = await rig.check()
        let checked = try XCTUnwrap(rig.subscription.lastCheckedAt)
        XCTAssertNil(rig.subscription.attention)

        // Core's retention decision takes the capture out. The file has not changed.
        rig.core.stopService()
        try rig.core.expireRetention(captureID: head)
        try rig.core.startService()
        _ = await rig.check()

        // No false "up to date": the checked time did not advance, the shortfall is recorded
        // as itself, and nothing was added to Core over its retention decision.
        let after = rig.subscription
        XCTAssertEqual(after.lastCheckedAt, checked)
        XCTAssertEqual(after.attention?.kind, .copyNotCurrent)
        XCTAssertEqual(after.attention?.code, CurrentCaptureShortfall.captureNotRetained.rawValue)
        XCTAssertEqual(after.currentSuccessfulCaptureID, head)
        XCTAssertEqual(try rig.core.captureIDs(documentID: documentID), [head])
        XCTAssertTrue(
            rig.environment.queue.allEntries().allSatisfy { $0.intent.surface != .automaticFile }
        )
        let history = try XCTUnwrap(rig.core.currentHistory(documentID: documentID))
        XCTAssertEqual(history.captures.first?.retentionState, .expired)

        // The Files window says the same, from the real stores and the real history.
        let model = FilesAndFoldersModel(
            workspaceName: "workspace-1",
            files: [after],
            coreHistory: CoreHistoryState(documentID: documentID, availability: .current(history))
        )
        let row = try XCTUnwrap(model.items(segment: .files).first)
        XCTAssertEqual(row.statusLabel, "Copy in Core not current")
        let text = try XCTUnwrap(model.detail(for: row).first { $0.label == "Latest check" }?.value)
        XCTAssertTrue(text.contains("expired or been deleted"), text)

        // A real change still flows, and Core's own policy decides: the new bytes are admitted
        // and the shortfall is replaced by the capture.
        try Data("changed".utf8).write(to: rig.original)
        _ = await rig.check()
        let delivered = await CaptureDeliveryCoordinator(
            queue: rig.environment.queue, transport: rig.core.delivery
        ).deliverPending()
        XCTAssertEqual(delivered.map(\.state), [.admitted])
        _ = await rig.environment.fileMonitoring.reconcileReceipts()
        XCTAssertNil(rig.subscription.attention)
        XCTAssertNotEqual(rig.subscription.currentSuccessfulCaptureID, head)
        XCTAssertEqual(try rig.core.captureIDs(documentID: documentID).count, 2)
    }

    func testARefreshCoreAdmitsButCannotProcessLeavesThePriorVersionSearchableAndIsItsOwnEntryThroughTheRealCore()
        async throws
    {
        let rig = try await rig(content: Data("baseline".utf8))
        let documentID = rig.watch.linkedDocumentID
        let first = try XCTUnwrap(rig.watch.currentSuccessfulCaptureID)

        // The refresh is bytes the text extractor cannot read.
        try Data([0xFF, 0xFE, 0x00, 0x80, 0xC3, 0x28, 0xA0, 0xA1]).write(to: rig.original)
        _ = await rig.check()
        let delivered = await CaptureDeliveryCoordinator(
            queue: rig.environment.queue, transport: rig.core.delivery
        ).deliverPending()
        XCTAssertEqual(delivered.map(\.state), [.admitted])
        _ = await rig.environment.fileMonitoring.reconcileReceipts()
        let second = try XCTUnwrap(rig.subscription.currentSuccessfulCaptureID)
        XCTAssertNotEqual(second, first)

        let history = try await settled(rig.core, documentID: documentID, extractions: 2)
        let failed = try XCTUnwrap(history.extractions.first { $0.captureID == second })
        XCTAssertNotEqual(failed.resultState, .succeeded)
        XCTAssertEqual(failed.publicationDisposition, .notPublished)
        XCTAssertFalse(failed.isCurrentExtraction)
        // The prior version is still the one retrieval serves, and its capture is retained.
        let served = try XCTUnwrap(history.extractions.first { $0.captureID == first })
        XCTAssertTrue(served.isCurrentExtraction)
        XCTAssertEqual(history.extraction.retrievalState, .retrievable)
        XCTAssertEqual(history.captures.first { $0.captureID == first }?.retentionState, .retained)
        XCTAssertEqual(history.source.currentCaptureID, second)

        // The Files window reads them as two entries, the newer one marked as not searchable.
        let model = FilesAndFoldersModel(
            workspaceName: "workspace-1",
            files: [rig.subscription],
            coreHistory: CoreHistoryState(documentID: documentID, availability: .current(history))
        )
        let facts = Dictionary(
            uniqueKeysWithValues: model.detail(
                for: try XCTUnwrap(model.items(segment: .files).first)
            ).map { ($0.label, $0.value) }
        )
        XCTAssertTrue(facts["Source capture 2"]?.contains("its processing") ?? false, "\(facts)")
        XCTAssertTrue(
            facts["Source capture 2"]?.contains("earlier searchable version is unchanged") ?? false
        )
        XCTAssertTrue(facts["Current searchable version"]?.hasPrefix("Extraction 1") ?? false)

        // And the unchanged bytes of that refresh are not "up to date": nothing searchable
        // stands behind them.
        _ = await rig.check()
        XCTAssertEqual(rig.subscription.attention?.code, CurrentCaptureShortfall.notRetrievable.rawValue)
    }
}
