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
        let environment = try DocumentIntakeEnvironment(stateRoot: state, bookmarkCodec: BookmarkCodec())
        let submission = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
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
        let grant = try XCTUnwrap(environment.accessStore.rootDescriptors().first)
        let run = try XCTUnwrap(environment.folderBulkRuns.all().first)
        XCTAssertEqual(run.rootBindingGeneration, grant.generation)
        XCTAssertTrue(entries.allSatisfy {
            $0.intent.originalBinding?.rootBindingGeneration == grant.generation
        })
    }

    func testEmptyOnlyNewWatchRetainsReviewedRootGrant() async throws {
        let root = try temporaryDirectory()
        let state = root.appendingPathComponent(".state", isDirectory: true)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec()
        )
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(
                mode: .keepUpdated,
                recursive: true,
                includeHidden: false,
                watchInitialContent: .onlyNewAndChanged
            )
        )

        let entries = try await submission.submit(plan)

        XCTAssertTrue(entries.isEmpty)
        let subscription = try XCTUnwrap(environment.folderSubscriptions.all().first)
        XCTAssertTrue(subscription.members.isEmpty)
        let grant = try XCTUnwrap(environment.accessStore.rootDescriptors().first)
        XCTAssertEqual(grant.rootRef, subscription.rootRef)
        XCTAssertEqual(grant.ownerRefs, [subscription.subscriptionID])
        XCTAssertEqual(grant.state, .connected)
        XCTAssertNoThrow(
            try environment.accessStore.withRootReadAccess(
                rootRef: grant.rootRef,
                expectedGeneration: grant.generation
            ) { resolved in
                XCTAssertEqual(resolved, root.resolvingSymlinksInPath().standardizedFileURL)
            }
        )

        _ = try await environment.folderMonitoring.stop(
            subscriptionID: subscription.subscriptionID
        )
        let released = try XCTUnwrap(environment.accessStore.rootDescriptors().first)
        XCTAssertTrue(released.ownerRefs.isEmpty)
        XCTAssertEqual(released.state, .disconnected)
        XCTAssertThrowsError(
            try environment.accessStore.withRootReadAccess(
                rootRef: released.rootRef,
                expectedGeneration: released.generation
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .disconnected) }
    }

    func testBulkAndWatchOwnersReleaseIndependentlyWhileManualMemberRouteSurvives() async throws {
        let root = try temporaryDirectory()
        try Data("retained".utf8).write(to: root.appendingPathComponent("retained.txt"))
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec()
        )
        let submission = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(mode: .keepUpdated, recursive: true, includeHidden: false)
        )
        let entries = try await submission.submit(plan)
        let run = try XCTUnwrap(environment.folderBulkRuns.all().first)
        let subscription = try XCTUnwrap(environment.folderSubscriptions.all().first)
        let member = try XCTUnwrap(run.members.first)
        XCTAssertEqual(
            Set(environment.accessStore.rootDescriptors().first?.ownerRefs ?? []),
            [run.runID, subscription.subscriptionID]
        )

        _ = try await environment.folderMonitoring.stop(subscriptionID: subscription.subscriptionID)
        var grant = try XCTUnwrap(environment.accessStore.rootDescriptors().first)
        XCTAssertEqual(grant.ownerRefs, [run.runID])
        XCTAssertEqual(grant.state, .connected)

        try admit(
            try XCTUnwrap(entries.first),
            captureID: "dcap-" + String(repeating: "d", count: 64),
            queue: environment.queue
        )
        _ = await environment.folderBulkCoordinator.reconcileReceipts()
        grant = try XCTUnwrap(environment.accessStore.rootDescriptors().first)
        XCTAssertTrue(grant.ownerRefs.isEmpty)
        XCTAssertEqual(grant.state, .connected)
        let text = try environment.accessStore.withReadAccess(
            accessRef: member.accessRef,
            expectedGeneration: member.bindingGeneration,
            expectedRootBindingGeneration: run.rootBindingGeneration
        ) { try String(contentsOf: $0, encoding: .utf8) }
        XCTAssertEqual(text, "retained")
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
        let environment = try DocumentIntakeEnvironment(stateRoot: state, bookmarkCodec: BookmarkCodec())
        let submission = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
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
        let grant = try XCTUnwrap(environment.accessStore.rootDescriptors().first)
        let run = try XCTUnwrap(environment.folderBulkRuns.all().first)
        let subscription = try XCTUnwrap(environment.folderSubscriptions.all().first)
        XCTAssertEqual(run.rootBindingGeneration, grant.generation)
        XCTAssertEqual(subscription.rootBindingGeneration, grant.generation)
        XCTAssertEqual(entries[0].intent.originalBinding?.rootBindingGeneration, grant.generation)
    }

    func testLegacyFolderLedgersDefaultRootGenerationAndFreezeItAcrossUpdates() async throws {
        let root = try temporaryDirectory()
        try Data("legacy".utf8).write(to: root.appendingPathComponent("legacy.txt"))
        let state = root.appendingPathComponent("state", isDirectory: true)
        let environment = try DocumentIntakeEnvironment(stateRoot: state, bookmarkCodec: BookmarkCodec())
        let submission = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(mode: .keepUpdated, recursive: true, includeHidden: false)
        )
        _ = try await submission.submit(plan)

        let runID = try XCTUnwrap(environment.folderBulkRuns.all().first?.runID)
        let subscriptionID = try XCTUnwrap(environment.folderSubscriptions.all().first?.subscriptionID)
        let runFile = state
            .appendingPathComponent("folder-bulk-runs", isDirectory: true)
            .appendingPathComponent("folder-bulk-runs-v1.json")
        let subscriptionFile = state
            .appendingPathComponent("folder-subscriptions", isDirectory: true)
            .appendingPathComponent("folder-subscriptions-v1.json")
        try removeRootBindingGeneration(from: runFile, collectionKey: "runs")
        try removeRootBindingGeneration(from: subscriptionFile, collectionKey: "subscriptions")

        let migratedRuns = try FolderBulkRunStore(
            stateDirectory: state.appendingPathComponent("folder-bulk-runs", isDirectory: true)
        )
        let migratedSubscriptions = try FolderSubscriptionStore(
            stateDirectory: state.appendingPathComponent("folder-subscriptions", isDirectory: true)
        )
        XCTAssertEqual(migratedRuns.run(id: runID)?.rootBindingGeneration, 1)
        XCTAssertEqual(
            migratedSubscriptions.subscription(id: subscriptionID)?.rootBindingGeneration,
            1
        )

        XCTAssertThrowsError(
            try migratedRuns.update(id: runID) { $0.rootBindingGeneration = 2 }
        ) { XCTAssertEqual($0 as? FolderBulkRunStoreError, .invalidState) }
        XCTAssertThrowsError(
            try migratedSubscriptions.update(id: subscriptionID) { $0.rootBindingGeneration = 2 }
        ) { XCTAssertEqual($0 as? FolderSubscriptionStoreError, .invalidState) }
        XCTAssertEqual(migratedRuns.run(id: runID)?.rootBindingGeneration, 1)
        XCTAssertEqual(
            migratedSubscriptions.subscription(id: subscriptionID)?.rootBindingGeneration,
            1
        )

        let reopenedRuns = try FolderBulkRunStore(
            stateDirectory: state.appendingPathComponent("folder-bulk-runs", isDirectory: true)
        )
        let reopenedSubscriptions = try FolderSubscriptionStore(
            stateDirectory: state.appendingPathComponent("folder-subscriptions", isDirectory: true)
        )
        XCTAssertEqual(reopenedRuns.run(id: runID)?.rootBindingGeneration, 1)
        XCTAssertEqual(
            reopenedSubscriptions.subscription(id: subscriptionID)?.rootBindingGeneration,
            1
        )
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

    func testUnusualFolderGrowthRequiresExplicitReviewBeforeAnyAutomaticReadPublication() async throws {
        let root = try temporaryDirectory()
        try Data("baseline".utf8).write(to: root.appendingPathComponent("baseline.txt"))
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
        _ = try await submission.submit(plan)
        for index in 0..<100 {
            try Data("new \(index)".utf8).write(
                to: root.appendingPathComponent(String(format: "new-%03d.txt", index))
            )
        }

        let review = await environment.folderMonitoring.reconcileAll()

        XCTAssertEqual(review.map(\.state), [.reviewRequired])
        XCTAssertTrue(environment.queue.allEntries().isEmpty)
        let paused = try XCTUnwrap(environment.folderSubscriptions.all().first)
        XCTAssertEqual(paused.reviewChangeCount, 100)
        XCTAssertEqual(paused.reviewExamples?.count, 10)

        _ = try await environment.folderMonitoring.resume(subscriptionID: paused.subscriptionID)
        let approved = await environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(approved.map(\.queued), [100])
        XCTAssertEqual(environment.queue.allEntries().count, 100)
    }

    func testUnusualDisappearanceRequiresReviewBeforeMarkingMembersAbsent() async throws {
        let root = try temporaryDirectory()
        var files: [URL] = []
        for index in 0..<100 {
            let file = root.appendingPathComponent(String(format: "existing-%03d.txt", index))
            try Data("baseline \(index)".utf8).write(to: file)
            files.append(file)
        }
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
        _ = try await submission.submit(plan)
        for file in files.prefix(20) { try FileManager.default.removeItem(at: file) }

        let review = await environment.folderMonitoring.reconcileAll()

        XCTAssertEqual(review.map(\.state), [.reviewRequired])
        let subscription = try XCTUnwrap(environment.folderSubscriptions.all().first)
        XCTAssertEqual(subscription.reviewChangeCount, 20)
        XCTAssertEqual(subscription.members.filter { $0.state == .absent }.count, 0)
        XCTAssertTrue(subscription.reviewExamples?.allSatisfy { $0.hasSuffix("(missing)") } == true)
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

    func testExactDuplicateFolderWatchIsRejectedForTheSameDestination() async throws {
        let root = try temporaryDirectory()
        try Data("baseline".utf8).write(to: root.appendingPathComponent("watch.txt"))
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
        _ = try await submission.submit(plan)

        do {
            _ = try await submission.submit(plan)
            XCTFail("duplicate folder watch should be rejected")
        } catch {
            XCTAssertEqual(error as? FolderCaptureSubmissionError, .duplicateWatch)
        }
        XCTAssertEqual(environment.folderSubscriptions.all().count, 1)
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

    func testRejectedFolderUpdateRequiresReviewAndCanBeRetried() async throws {
        let root = try temporaryDirectory()
        let watched = root.appendingPathComponent("watch.txt")
        try Data("baseline".utf8).write(to: watched)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec()
        )
        let submission = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(mode: .keepUpdated, recursive: true, includeHidden: false)
        )
        let baseline = try await submission.submit(plan)
        try admit(
            baseline[0],
            captureID: "dcap-" + String(repeating: "a", count: 64),
            queue: environment.queue
        )
        _ = await environment.folderMonitoring.reconcileAll()
        try Data("changed".utf8).write(to: watched)
        _ = await environment.folderMonitoring.reconcileAll()
        let automatic = try XCTUnwrap(environment.queue.allEntries().first {
            $0.intent.surface == .automaticFolder && $0.intent.itemId != baseline[0].intent.itemId
        })
        _ = try environment.queue.markFailed(
            itemID: automatic.intent.itemId,
            failureCode: "policy_rejected"
        )

        _ = await environment.folderMonitoring.reconcileReceipts()
        let review = try XCTUnwrap(environment.folderSubscriptions.all().first)
        XCTAssertEqual(review.state, .reviewRequired)
        XCTAssertNil(review.members.first?.lastObservedDigest)

        _ = try await environment.folderMonitoring.resume(subscriptionID: review.subscriptionID)
        _ = await environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(
            environment.queue.allEntries().filter {
                $0.intent.surface == .automaticFolder && $0.intent.itemId != baseline[0].intent.itemId
            }.count,
            2
        )
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

    private func removeRootBindingGeneration(from file: URL, collectionKey: String) throws {
        let data = try Data(contentsOf: file)
        var document = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        var values = try XCTUnwrap(document[collectionKey] as? [[String: Any]])
        for index in values.indices {
            values[index].removeValue(forKey: "rootBindingGeneration")
        }
        document[collectionKey] = values
        try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
            .write(to: file, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
    }
}
