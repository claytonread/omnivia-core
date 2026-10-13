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

    func testPlanSkipsDefaultTemporaryAndToolTrees() throws {
        let root = try temporaryDirectory()
        try Data("keep".utf8).write(to: root.appendingPathComponent("keep.md"))
        try Data("partial".utf8).write(to: root.appendingPathComponent("download.part"))
        let dependency = root.appendingPathComponent("node_modules", isDirectory: true)
        try FileManager.default.createDirectory(at: dependency, withIntermediateDirectories: true)
        try Data("ignore".utf8).write(to: dependency.appendingPathComponent("readme.md"))
        let submission = try FolderCaptureSubmission(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            workspaceRef: "workspace-1",
            bookmarkCodec: BookmarkCodec()
        )

        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(mode: .importOnce, recursive: true, includeHidden: true)
        )

        XCTAssertEqual(plan.members.map(\.displayName), ["keep.md"])
        XCTAssertGreaterThanOrEqual(plan.skippedExcluded, 1)
        XCTAssertGreaterThanOrEqual(plan.examinedEntries, 3)
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

    func testLargeFolderRunProgressesInDurableBoundedBatches() async throws {
        let root = try temporaryDirectory()
        for index in 0..<12 {
            try Data("file \(index)".utf8).write(
                to: root.appendingPathComponent(String(format: "%02d.txt", index))
            )
        }
        let state = root.appendingPathComponent("state", isDirectory: true)
        let environment = try DocumentIntakeEnvironment(stateRoot: state, bookmarkCodec: BookmarkCodec())
        let submission = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(mode: .importOnce, recursive: true, includeHidden: false)
        )

        let first = try await submission.submit(plan)

        XCTAssertEqual(first.count, 10)
        var run = try XCTUnwrap(environment.folderBulkRuns.all().first)
        XCTAssertEqual(run.plannedCount, 2)
        XCTAssertEqual(run.publishedCount, 10)
        for (index, entry) in first.enumerated() {
            try admit(
                entry,
                captureID: "dcap-" + String(repeating: String(format: "%x", index % 16), count: 64),
                queue: environment.queue
            )
        }

        let second = await environment.folderBulkCoordinator.advance(runID: run.runID)

        XCTAssertEqual(second.newlyPublished.count, 2)
        run = try XCTUnwrap(environment.folderBulkRuns.run(id: run.runID))
        XCTAssertEqual(run.admittedCount, 10)
        XCTAssertEqual(run.publishedCount, 2)
        for (index, entry) in second.newlyPublished.enumerated() {
            try admit(
                entry,
                captureID: "dcap-" + String(repeating: index == 0 ? "e" : "f", count: 64),
                queue: environment.queue
            )
        }
        _ = await environment.folderBulkCoordinator.reconcileReceipts()
        XCTAssertEqual(environment.folderBulkRuns.run(id: run.runID)?.state, .complete)

        let reopened = try DocumentIntakeEnvironment(stateRoot: state, bookmarkCodec: BookmarkCodec())
        XCTAssertEqual(reopened.folderBulkRuns.run(id: run.runID)?.state, .complete)
    }

    func testBulkPauseAndStopAffectOnlyUnpublishedMembers() async throws {
        let root = try temporaryDirectory()
        for index in 0..<12 {
            try Data("file \(index)".utf8).write(
                to: root.appendingPathComponent(String(format: "%02d.txt", index))
            )
        }
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec()
        )
        let submission = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(mode: .importOnce, recursive: true, includeHidden: false)
        )
        let first = try await submission.submit(plan)
        let runID = try XCTUnwrap(environment.folderBulkRuns.all().first?.runID)

        _ = try await environment.folderBulkCoordinator.pause(runID: runID)
        let paused = await environment.folderBulkCoordinator.advance(runID: runID)
        XCTAssertTrue(paused.newlyPublished.isEmpty)
        XCTAssertEqual(paused.state, .paused)
        _ = try await environment.folderBulkCoordinator.resume(runID: runID)
        _ = try await environment.folderBulkCoordinator.stopRemaining(runID: runID)

        let stopped = try XCTUnwrap(environment.folderBulkRuns.run(id: runID))
        XCTAssertEqual(stopped.members.filter { $0.state == .stopped }.count, 2)
        XCTAssertEqual(stopped.members.filter { $0.state == .published }.count, first.count)
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

    func testOnlyNewAndChangedEstablishesLocalBaselineWithoutCapturingExistingFiles() async throws {
        let root = try temporaryDirectory()
        let existing = root.appendingPathComponent("existing.txt")
        try Data("baseline".utf8).write(to: existing)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec()
        )
        let submission = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(
                mode: .keepUpdated,
                recursive: true,
                includeHidden: false,
                watchInitialContent: .onlyNewAndChanged
            )
        )

        let baselineEntries = try await submission.submit(plan)
        let initialCheck = await environment.folderMonitoring.reconcileAll()

        XCTAssertTrue(baselineEntries.isEmpty)
        XCTAssertTrue(environment.queue.allEntries().isEmpty)
        XCTAssertEqual(environment.folderSubscriptions.all().map(\.state), [.active])
        XCTAssertEqual(initialCheck.map(\.unchanged), [1])
        XCTAssertEqual(initialCheck.map(\.queued), [0])

        try Data("changed".utf8).write(to: existing)
        try Data("new".utf8).write(to: root.appendingPathComponent("new.md"))
        let changed = await environment.folderMonitoring.reconcileAll()

        XCTAssertEqual(changed.map(\.queued), [2])
        XCTAssertEqual(
            Set(environment.queue.allEntries().compactMap(\.intent.displayName)),
            ["existing.txt", "new.md"]
        )
        XCTAssertTrue(environment.queue.allEntries().allSatisfy {
            $0.intent.basePrecondition == .expected(nil)
        })
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
