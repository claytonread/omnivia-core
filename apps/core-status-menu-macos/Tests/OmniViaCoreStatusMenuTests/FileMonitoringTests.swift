import DocumentIntakeMacOS
import Foundation
import XCTest

final class FileMonitoringTests: XCTestCase {
    private struct BookmarkCodec: SecurityScopedBookmarkCoding {
        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
        }
        func startAccessing(_ url: URL) -> Bool { true }
        func stopAccessing(_ url: URL) {}
    }

    func testIndividualWatchWaitsForAdmissionThenQueuesChangedVersionAgainstCurrentHead() async throws {
        let root = try temporaryDirectory()
        let state = root.appendingPathComponent("state", isDirectory: true)
        let original = root.appendingPathComponent("watched.txt")
        try Data("baseline".utf8).write(to: original)
        let environment = try DocumentIntakeEnvironment(stateRoot: state, bookmarkCodec: BookmarkCodec())
        let submission = ManualCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        let baseline = try await submission.submit([
            ManualCaptureCandidate(url: original, keepUpdated: true),
        ])
        try Data("changed too early".utf8).write(to: original)

        let waiting = await environment.fileMonitoring.reconcileAll()
        XCTAssertEqual(waiting.map(\.state), [.waitingForCore])
        XCTAssertEqual(environment.queue.allEntries().count, 1)

        let baselineCaptureID = "dcap-" + String(repeating: "a", count: 64)
        try admit(baseline[0], captureID: baselineCaptureID, queue: environment.queue)
        let changed = await environment.fileMonitoring.reconcileAll()

        let observationURLs = await environment.fileMonitoring.activeObservationRoots().map(\.url)
        XCTAssertEqual(observationURLs, [root])

        XCTAssertEqual(changed.map(\.queued), [true])
        let automatic = try XCTUnwrap(
            environment.queue.allEntries().first { $0.intent.surface == .automaticFile }
        )
        XCTAssertEqual(automatic.intent.intentKind, .automaticCapture)
        XCTAssertEqual(automatic.intent.basePrecondition, .expected(baselineCaptureID))
        XCTAssertNotNil(automatic.intent.consent)

        let changedCaptureID = "dcap-" + String(repeating: "c", count: 64)
        try admit(automatic, captureID: changedCaptureID, queue: environment.queue)
        _ = await environment.fileMonitoring.reconcileAll()
        XCTAssertEqual(
            environment.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            1
        )
        XCTAssertEqual(environment.fileSubscriptions.all().first?.currentSuccessfulCaptureID, changedCaptureID)

        let reopened = try DocumentIntakeEnvironment(stateRoot: state, bookmarkCodec: BookmarkCodec())
        XCTAssertEqual(reopened.fileSubscriptions.all().map(\.state), [.active])
    }

    func testRejectedAutomaticVersionPausesAndCanBeExplicitlyRetried() async throws {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("watched.txt")
        try Data("baseline".utf8).write(to: original)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec()
        )
        let baseline = try await ManualCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        ).submit([ManualCaptureCandidate(url: original, keepUpdated: true)])
        try admit(
            baseline[0],
            captureID: "dcap-" + String(repeating: "a", count: 64),
            queue: environment.queue
        )
        try Data("changed".utf8).write(to: original)
        _ = await environment.fileMonitoring.reconcileAll()
        let rejected = try XCTUnwrap(
            environment.queue.allEntries().first { $0.intent.surface == .automaticFile }
        )
        _ = try environment.queue.markFailed(itemID: rejected.intent.itemId, failureCode: "policy_rejected")

        _ = await environment.fileMonitoring.reconcileReceipts()
        let paused = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(paused.state, .paused)
        XCTAssertNil(paused.lastObservedDigest)
        XCTAssertNil(paused.lastQueuedItemID)

        _ = try await environment.fileMonitoring.resume(subscriptionID: paused.subscriptionID)
        _ = await environment.fileMonitoring.reconcileAll()
        XCTAssertEqual(
            environment.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            2
        )
    }

    private func admit(
        _ entry: CaptureQueueEntry,
        captureID: String,
        queue: CaptureIntentQueue
    ) throws {
        let digest = "sha256:" + String(repeating: "d", count: 64)
        let staging = try queue.beginStage(
            itemID: entry.intent.itemId,
            requestDigest: digest,
            descriptorDigest: digest
        )
        _ = try queue.markAdmitted(
            itemID: entry.intent.itemId,
            attemptGeneration: staging.attemptGeneration,
            captureID: captureID
        )
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-file-monitor-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
