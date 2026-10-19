@testable import DocumentIntakeMacOS
import Foundation
import XCTest

/// MON-32: Core retention/removal is a canonical automatic-capture boundary,
/// not a fact local to whichever route happened to observe it first.
final class AutomaticCaptureSuppressionTests: XCTestCase {
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
        var reader: @Sendable () -> Int64 { { [self] in lock.withLock { value } } }
    }

    private final class Verifier: CurrentCaptureVerifying, @unchecked Sendable {
        private let lock = NSLock()
        private var value: CurrentCaptureVerdict = .current
        func answer(_ answer: CurrentCaptureVerdict) { lock.withLock { value = answer } }
        func verify(
            documentID _: String,
            captureID _: String?,
            observedDigest _: String
        ) -> CurrentCaptureVerdict {
            lock.withLock { value }
        }
    }

    func testRemovalSuppressionSurvivesOverlapRenameRestartAndPeriodicChecks() async throws {
        let root = try temporaryDirectory()
        let watched = root.appendingPathComponent("watched", isDirectory: true)
        try FileManager.default.createDirectory(at: watched, withIntermediateDirectories: true)
        let original = watched.appendingPathComponent("brief.txt")
        try Data("retained bytes".utf8).write(to: original)
        let state = root.appendingPathComponent("state", isDirectory: true)
        let clock = TestClock()
        let verifier = Verifier()
        let environment = try makeEnvironment(
            state: state,
            clock: clock,
            verifier: verifier
        )

        let folder = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        _ = try await folder.submit(
            try folder.plan(
                root: watched,
                policy: FolderCapturePolicy(
                    mode: .keepUpdated,
                    recursive: true,
                    includeHidden: false
                )
            )
        )
        _ = try await ManualCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        ).submit([ManualCaptureCandidate(url: original, keepUpdated: true)])

        let initial = try XCTUnwrap(environment.queue.allEntries().first)
        XCTAssertEqual(environment.queue.allEntries().count, 1)
        try admit(initial, queue: environment.queue)
        _ = await environment.folderMonitoring.reconcileAll()
        _ = await environment.fileMonitoring.reconcileReceipts()

        let sourceIdentity = try XCTUnwrap(
            environment.folderSubscriptions.all().first?.members.first?.requestedSourceIdentity
        )
        XCTAssertEqual(
            environment.fileSubscriptions.all().first?.requestedSourceIdentity,
            sourceIdentity
        )

        // The elected folder route learns Core removed the current bytes. The
        // decision becomes one canonical source/digest tombstone.
        verifier.answer(.notCurrent(.captureNotRetained))
        _ = await environment.folderMonitoring.reconcileAll()
        let suppression = try XCTUnwrap(environment.automaticSuppressions.all().first)
        XCTAssertEqual(suppression.requestedSourceIdentity, sourceIdentity)
        XCTAssertEqual(suppression.reason, .captureNotRetained)
        XCTAssertEqual(environment.queue.allEntries().count, 1)

        // An overlapping direct route becomes the scheduler. It sees the same
        // exact bytes and consumes its check without reconstructing evidence.
        let folderID = try XCTUnwrap(environment.folderSubscriptions.all().first?.subscriptionID)
        _ = try await environment.folderMonitoring.pause(subscriptionID: folderID)
        let directID = try XCTUnwrap(environment.fileSubscriptions.all().first?.subscriptionID)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [directID])
        clock.advance(3_000)
        let directReports = await environment.fileMonitoring.reconcileAll()
        XCTAssertEqual(directReports.map(\.queued), [false])
        XCTAssertEqual(environment.queue.allEntries().count, 1)

        // The same native file is renamed, then rediscovered through the root.
        // Its canonical identity and removed digest survive the path change.
        _ = try await environment.folderMonitoring.resume(subscriptionID: folderID)
        let renamed = watched.appendingPathComponent("renamed.txt")
        try FileManager.default.moveItem(at: original, to: renamed)
        _ = await environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(environment.queue.allEntries().count, 1)
        let present = try XCTUnwrap(
            environment.folderSubscriptions.all().first?.members.first { $0.state == .present }
        )
        XCTAssertEqual(present.requestedSourceIdentity, sourceIdentity)
        XCTAssertEqual(present.lastObservedDigest, suppression.observedDigest)
        XCTAssertNil(present.lastQueuedItemID)

        // A fresh process loads the tombstone. The same full reconciliation
        // used by the 24-hour verifier still queues nothing.
        let restarted = try makeEnvironment(
            state: state,
            clock: clock,
            verifier: verifier
        )
        XCTAssertEqual(restarted.automaticSuppressions.all(), [suppression])
        _ = await restarted.folderMonitoring.reconcileAll()
        XCTAssertEqual(restarted.queue.allEntries().count, 1)
        XCTAssertEqual(restarted.automaticSuppressions.all(), [suppression])
    }

    func testOnlyGovernedRemovalOutcomesBecomeSuppression() throws {
        let root = try temporaryDirectory()
        let store = try AutomaticCaptureSuppressionStore(
            stateDirectory: root.appendingPathComponent("suppressions", isDirectory: true)
        )
        let digest = "sha256:" + String(repeating: "a", count: 64)
        XCTAssertNil(try store.record(
            workspaceRef: "workspace-1",
            requestedSourceIdentity: "source-1",
            observedDigest: digest,
            reason: .notRetrievable,
            recordedAt: "2026-10-17T00:00:00Z"
        ))
        XCTAssertTrue(store.all().isEmpty)
    }

    private func makeEnvironment(
        state: URL,
        clock: TestClock,
        verifier: Verifier
    ) throws -> DocumentIntakeEnvironment {
        try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec(),
            monotonicClock: clock.reader,
            currentCapture: verifier
        )
    }

    private func admit(_ entry: CaptureQueueEntry, queue: CaptureIntentQueue) throws {
        let digest = "sha256:" + String(repeating: "c", count: 64)
        let stage = try queue.beginStage(
            itemID: entry.intent.itemId,
            requestDigest: digest,
            descriptorDigest: digest
        )
        _ = try queue.markAdmitted(
            itemID: entry.intent.itemId,
            attemptGeneration: stage.attemptGeneration,
            captureID: "dcap-" + String(repeating: "a", count: 64)
        )
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-suppression-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
