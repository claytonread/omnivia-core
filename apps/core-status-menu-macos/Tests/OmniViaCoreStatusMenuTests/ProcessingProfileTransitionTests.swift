@testable import DocumentIntakeMacOS
import Foundation
import XCTest

/// MON-31: a profile transition is prospective. New bytes freeze the approved
/// profile of the environment that captured them; retained captures and an
/// excluded baseline do not become work merely because that profile changed.
final class ProcessingProfileTransitionTests: XCTestCase {
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

    private let oldProfile = ProcessingProfileBinding(
        profileRef: "document-default",
        revision: "1"
    )
    private let newProfile = ProcessingProfileBinding(
        profileRef: "document-default",
        revision: "2"
    )

    func testFolderProfileTransitionDoesNotReprocessRetainedBytes() async throws {
        let root = try temporaryDirectory()
        let watched = root.appendingPathComponent("watched", isDirectory: true)
        try FileManager.default.createDirectory(at: watched, withIntermediateDirectories: true)
        let file = watched.appendingPathComponent("brief.txt")
        try Data("baseline".utf8).write(to: file)
        let state = root.appendingPathComponent("state", isDirectory: true)
        let first = try environment(state: state, profile: oldProfile)
        let folder = FolderCaptureSubmission(environment: first, workspaceRef: "workspace-1")
        _ = try await folder.submit(
            try folder.plan(
                root: watched,
                policy: FolderCapturePolicy(mode: .keepUpdated, recursive: true, includeHidden: false)
            )
        )
        XCTAssertEqual(first.queue.allEntries().map(\.intent.processingProfile), [oldProfile])
        for entry in first.queue.allEntries() { try admit(entry, queue: first.queue) }
        _ = await first.folderMonitoring.reconcileAll()

        let reopened = try environment(state: state, profile: newProfile)
        let before = Set(reopened.queue.allEntries().map(\.intent.itemId))
        _ = await reopened.folderMonitoring.reconcileAll()
        XCTAssertEqual(Set(reopened.queue.allEntries().map(\.intent.itemId)), before)

        try Data("changed after the profile transition".utf8).write(to: file)
        _ = await reopened.folderMonitoring.reconcileAll()
        let added = reopened.queue.allEntries().filter { !before.contains($0.intent.itemId) }
        XCTAssertEqual(added.map(\.intent.processingProfile), [newProfile])
    }

    func testExcludedBaselineStaysExcludedUntilItsBytesChange() async throws {
        let root = try temporaryDirectory()
        let watched = root.appendingPathComponent("watched", isDirectory: true)
        try FileManager.default.createDirectory(at: watched, withIntermediateDirectories: true)
        let file = watched.appendingPathComponent("existing.txt")
        try Data("excluded baseline".utf8).write(to: file)
        let state = root.appendingPathComponent("state", isDirectory: true)
        let first = try environment(state: state, profile: oldProfile)
        let folder = FolderCaptureSubmission(environment: first, workspaceRef: "workspace-1")
        _ = try await folder.submit(
            try folder.plan(
                root: watched,
                policy: FolderCapturePolicy(
                    mode: .keepUpdated,
                    recursive: true,
                    includeHidden: false,
                    watchInitialContent: .onlyNewAndChanged
                )
            )
        )
        XCTAssertTrue(first.queue.allEntries().isEmpty)

        let reopened = try environment(state: state, profile: newProfile)
        _ = await reopened.folderMonitoring.reconcileAll()
        XCTAssertTrue(reopened.queue.allEntries().isEmpty)

        try Data("now changed".utf8).write(to: file)
        _ = await reopened.folderMonitoring.reconcileAll()
        XCTAssertEqual(reopened.queue.allEntries().map(\.intent.processingProfile), [newProfile])
    }

    func testManualAndAutomaticFileCapturesUseTheNewApprovedProfile() async throws {
        let root = try temporaryDirectory()
        let state = root.appendingPathComponent("state", isDirectory: true)
        let manualFile = root.appendingPathComponent("manual.txt")
        try Data("manual".utf8).write(to: manualFile)
        let current = try environment(state: state, profile: newProfile)
        _ = try await ManualCaptureSubmission(
            environment: current,
            workspaceRef: "workspace-1"
        ).submit([ManualCaptureCandidate(url: manualFile)])
        XCTAssertEqual(current.queue.allEntries().map(\.intent.processingProfile), [newProfile])

        let watchedFile = root.appendingPathComponent("watched.txt")
        try Data("baseline".utf8).write(to: watchedFile)
        let legacyState = root.appendingPathComponent("legacy-state", isDirectory: true)
        let oldClock = TestClock()
        let old = try environment(state: legacyState, profile: oldProfile, clock: oldClock)
        _ = try await ManualCaptureSubmission(environment: old, workspaceRef: "workspace-1")
            .submit([ManualCaptureCandidate(url: watchedFile, keepUpdated: true)])
        for entry in old.queue.allEntries() { try admit(entry, queue: old.queue) }
        _ = await old.fileMonitoring.reconcileAll()

        let newClock = TestClock()
        let upgraded = try environment(state: legacyState, profile: newProfile, clock: newClock)
        let subscriptionID = try XCTUnwrap(upgraded.fileSubscriptions.all().first?.subscriptionID)
        try Data("changed".utf8).write(to: watchedFile)
        await upgraded.fileMonitoring.markDirty(subscriptionIDs: [subscriptionID])
        newClock.advance(3_000)
        _ = await upgraded.fileMonitoring.reconcileAll()

        XCTAssertEqual(
            upgraded.queue.allEntries().filter { $0.state == .pending }
                .map(\.intent.processingProfile),
            [newProfile]
        )
    }

    private func environment(
        state: URL,
        profile: ProcessingProfileBinding,
        clock: TestClock? = nil
    ) throws -> DocumentIntakeEnvironment {
        try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec(),
            monotonicClock: clock?.reader,
            processingProfile: profile
        )
    }

    private func admit(_ entry: CaptureQueueEntry, queue: CaptureIntentQueue) throws {
        let digest = "sha256:" + String(repeating: "f", count: 64)
        let staging = try queue.beginStage(
            itemID: entry.intent.itemId,
            requestDigest: digest,
            descriptorDigest: digest
        )
        _ = try queue.markAdmitted(
            itemID: entry.intent.itemId,
            attemptGeneration: staging.attemptGeneration,
            captureID: "dcap-" + String(repeating: "a", count: 64)
        )
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-profile-transition-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
