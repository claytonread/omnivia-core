@testable import DocumentIntakeMacOS
import Foundation
import XCTest

final class FolderCaptureSubmissionTests: XCTestCase {
    private struct BookmarkCodec: SecurityScopedBookmarkCoding {
        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
        }
        func startAccessing(_ url: URL) -> Bool { true }
        func stopAccessing(_ url: URL) {}
    }

    func testPlanAppliesRecursiveHiddenAndSupportedFormatScope() throws {
        let root = try temporaryDirectory()
        try Data("root".utf8).write(to: root.appendingPathComponent("root.txt"))
        try Data("hidden".utf8).write(to: root.appendingPathComponent(".hidden.txt"))
        try Data("unsupported".utf8).write(to: root.appendingPathComponent("archive.bin"))
        let nested = root.appendingPathComponent("nested", isDirectory: true)
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        try Data("nested".utf8).write(to: nested.appendingPathComponent("brief.pdf"))
        let submission = try FolderCaptureSubmission(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            workspaceRef: "workspace-1",
            bookmarkCodec: BookmarkCodec()
        )

        let topLevel = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(
                mode: .importOnce,
                recursive: false,
                includeHidden: false
            )
        )
        let recursive = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(
                mode: .importOnce,
                recursive: true,
                includeHidden: true
            )
        )

        XCTAssertEqual(topLevel.members.map(\.displayName), ["root.txt"])
        XCTAssertEqual(topLevel.skippedHidden, 1)
        XCTAssertEqual(topLevel.skippedUnsupported, 1)
        XCTAssertEqual(
            recursive.members.map(\.displayName),
            [".hidden.txt", "nested/brief.pdf", "root.txt"]
        )
    }

    func testImportOncePublishesFolderLineageAndFrozenMembers() async throws {
        let root = try temporaryDirectory()
        try Data("first".utf8).write(to: root.appendingPathComponent("one.txt"))
        try Data("second".utf8).write(to: root.appendingPathComponent("two.md"))
        let state = root.appendingPathComponent("state", isDirectory: true)
        let submission = try FolderCaptureSubmission(
            stateRoot: state,
            workspaceRef: "workspace-1",
            bookmarkCodec: BookmarkCodec()
        )
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(
                mode: .importOnce,
                recursive: true,
                includeHidden: false
            )
        )

        let entries = try await submission.submit(plan)

        XCTAssertEqual(entries.count, 2)
        XCTAssertTrue(entries.allSatisfy { $0.intent.surface == .folderImport })
        XCTAssertTrue(entries.allSatisfy { $0.intent.intentKind == .folderCapture })
        XCTAssertTrue(entries.allSatisfy { $0.intent.bulkOrigin != nil })
        XCTAssertTrue(entries.allSatisfy { $0.intent.consent == nil })
        XCTAssertEqual(Set(entries.compactMap { $0.intent.originalBinding?.rootAccessGroupId }).count, 1)
    }

    func testKeepUpdatedBaselineFreezesStandingConsent() async throws {
        let root = try temporaryDirectory()
        try Data("watch me".utf8).write(to: root.appendingPathComponent("watch.txt"))
        let state = root.appendingPathComponent("state", isDirectory: true)
        let submission = try FolderCaptureSubmission(
            stateRoot: state,
            workspaceRef: "workspace-1",
            bookmarkCodec: BookmarkCodec()
        )
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(
                mode: .keepUpdated,
                recursive: true,
                includeHidden: false
            )
        )

        let entries = try await submission.submit(plan)

        XCTAssertEqual(entries.count, 1)
        XCTAssertEqual(entries[0].intent.surface, .automaticFolder)
        XCTAssertEqual(entries[0].intent.intentKind, .folderCapture)
        XCTAssertNotNil(entries[0].intent.consent)
        XCTAssertNotNil(entries[0].intent.bulkOrigin)
    }

    func testWatchDoesNotReadOrQueueChangesBeforeBaselineAdmission() async throws {
        let root = try temporaryDirectory()
        let watched = root.appendingPathComponent("watch.txt")
        try Data("baseline".utf8).write(to: watched)
        let state = root.appendingPathComponent("state", isDirectory: true)
        let environment = try DocumentIntakeEnvironment(stateRoot: state, bookmarkCodec: BookmarkCodec())
        let submission = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(mode: .keepUpdated, recursive: true, includeHidden: false)
        )
        let baseline = try await submission.submit(plan)
        try Data("changed before admission".utf8).write(to: watched)
        try Data("new before admission".utf8).write(to: root.appendingPathComponent("new.md"))

        let reports = await environment.folderMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.state), [.waitingForCore])
        XCTAssertEqual(environment.queue.allEntries().map(\.intent.itemId), baseline.map(\.intent.itemId))
    }

    func testAdmittedWatchPersistsAndQueuesChangedAndNewMembersWithCurrentBase() async throws {
        let root = try temporaryDirectory()
        let watched = root.appendingPathComponent("watch.txt")
        try Data("baseline".utf8).write(to: watched)
        let state = root.appendingPathComponent("state", isDirectory: true)
        let environment = try DocumentIntakeEnvironment(stateRoot: state, bookmarkCodec: BookmarkCodec())
        let submission = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(mode: .keepUpdated, recursive: true, includeHidden: false)
        )
        let baseline = try await submission.submit(plan)
        let captureID = "dcap-" + String(repeating: "a", count: 64)
        try admit(baseline[0], captureID: captureID, queue: environment.queue)

        let activation = await environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(activation.map(\.state), [.active])
        XCTAssertEqual(activation.map(\.queued), [0])

        try Data("changed".utf8).write(to: watched)
        try Data("new".utf8).write(to: root.appendingPathComponent("new.md"))
        let reports = await environment.folderMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.queued), [2])
        let baselineIDs = Set(baseline.map(\.intent.itemId))
        let automatic = environment.queue.allEntries().filter {
            $0.intent.surface == .automaticFolder && !baselineIDs.contains($0.intent.itemId)
        }
        XCTAssertEqual(automatic.count, 2)
        let changed = try XCTUnwrap(automatic.first { $0.intent.displayName == "watch.txt" })
        let added = try XCTUnwrap(automatic.first { $0.intent.displayName == "new.md" })
        XCTAssertEqual(changed.intent.basePrecondition, .expected(captureID))
        XCTAssertEqual(added.intent.basePrecondition, .expected(nil))
        XCTAssertEqual(changed.intent.consent?.dirtyEpoch, 2)

        let changedCaptureID = "dcap-" + String(repeating: "c", count: 64)
        try admit(changed, captureID: changedCaptureID, queue: environment.queue)
        _ = await environment.folderMonitoring.reconcileAll()
        let afterAdmission = environment.queue.allEntries().filter {
            $0.intent.surface == .automaticFolder && !baselineIDs.contains($0.intent.itemId)
        }
        XCTAssertEqual(afterAdmission.count, 2, "an admitted unchanged version must not be queued again")
        XCTAssertEqual(
            environment.folderSubscriptions.all().first?.members
                .first(where: { $0.displayName == "watch.txt" })?
                .currentSuccessfulCaptureID,
            changedCaptureID
        )

        let reopened = try DocumentIntakeEnvironment(stateRoot: state, bookmarkCodec: BookmarkCodec())
        XCTAssertEqual(reopened.folderSubscriptions.all().map(\.state), [.active])
        let ledger = try String(
            contentsOf: state
                .appendingPathComponent("folder-subscriptions", isDirectory: true)
                .appendingPathComponent("folder-subscriptions-v1.json"),
            encoding: .utf8
        )
        XCTAssertFalse(ledger.contains(root.path))
    }

    private func admit(
        _ entry: CaptureQueueEntry,
        captureID: String,
        queue: CaptureIntentQueue
    ) throws {
        let digest = "sha256:" + String(repeating: "b", count: 64)
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
            .appendingPathComponent("omnivia-folder-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
