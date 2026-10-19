import CryptoKit
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

    /// The wall clock the periodic folder schedule reads, moved by hand: the design's intervals
    /// are 15 minutes and 24 hours, and neither is waited out in a test.
    private final class Clock: @unchecked Sendable {
        private let lock = NSLock()
        private var value: Date

        init(_ value: Date) { self.value = value }

        var now: Date { lock.withLock { value } }

        func advance(_ seconds: TimeInterval) { lock.withLock { value += seconds } }
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
        XCTAssertEqual(grant.state, .active)
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
        XCTAssertEqual(grant.state, .active)

        try admit(
            try XCTUnwrap(entries.first),
            captureID: "dcap-" + String(repeating: "d", count: 64),
            queue: environment.queue
        )
        _ = await environment.folderBulkCoordinator.reconcileReceipts()
        grant = try XCTUnwrap(environment.accessStore.rootDescriptors().first)
        XCTAssertTrue(grant.ownerRefs.isEmpty)
        XCTAssertEqual(grant.state, .active)
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

    /// MON-06: a new-and-changed baseline at all three of the moments the gate
    /// names. The existing files are recorded locally and produce no Core evidence
    /// at creation, again after a restart, and again at the first 24-hour content
    /// verification — which is the one pass that re-reads their bytes.
    func testAnOnlyNewAndChangedBaselineMakesNoEvidenceAtCreationRestartOrTheFirstContentPass() async throws {
        let root = try temporaryDirectory()
        let state = root.appendingPathComponent("state", isDirectory: true)
        try Data("baseline".utf8).write(to: root.appendingPathComponent("existing.txt"))
        try Data("other".utf8).write(to: root.appendingPathComponent("other.md"))
        let clock = Clock(Date(timeIntervalSince1970: 1_760_000_000))
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec(),
            reconciliationClock: { clock.now }
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

        // At creation: the baseline is local only. Nothing is queued, so nothing
        // can reach Core, and no member claims a capture.
        let created = try await submission.submit(plan)
        XCTAssertTrue(created.isEmpty)
        XCTAssertTrue(environment.queue.allEntries().isEmpty)
        let established = try XCTUnwrap(environment.folderSubscriptions.all().first)
        XCTAssertEqual(established.state, .active)
        XCTAssertEqual(established.members.count, 2)
        XCTAssertTrue(established.members.allSatisfy { $0.baselineItemID == nil })
        XCTAssertTrue(established.members.allSatisfy { $0.lastQueuedItemID == nil })
        XCTAssertTrue(established.members.allSatisfy { $0.currentSuccessfulCaptureID == nil })
        XCTAssertTrue(established.members.allSatisfy { $0.lastObservedDigest != nil })

        // After a restart: the same durable state, read by a new process. The
        // pending establishment is already finished, and a full scan recognises
        // every baseline member as unchanged rather than as new.
        let restarted = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec(),
            reconciliationClock: { clock.now }
        )
        let pending = await restarted.reconcilePendingFolderEstablishment()
        XCTAssertTrue(pending.isEmpty)
        let afterRestart = await restarted.folderMonitoring.reconcileAll()
        XCTAssertEqual(afterRestart.map(\.queued), [0])
        XCTAssertEqual(afterRestart.map(\.unchanged), [2])
        XCTAssertTrue(restarted.queue.allEntries().isEmpty)
        XCTAssertEqual(
            restarted.folderSubscriptions.all().first?.members
                .compactMap(\.currentSuccessfulCaptureID).count,
            0
        )

        // The first scheduled passes, the 24-hour content verification among
        // them: it re-reads exactly these baseline members' bytes and finds them
        // unchanged, so there is nothing to hand to the capture path.
        let first = try restarted.folderReconciliation.schedule()
        XCTAssertEqual(
            Set(first.map(\.kind)),
            Set(FolderReconciliationKind.allCases)
        )
        let firstRuns = await restarted.folderReconciliationExecutor.run(first)
        XCTAssertEqual(firstRuns.map(\.outcome), [.complete, .complete])
        XCTAssertTrue(firstRuns.allSatisfy { !$0.drifted })
        // Both baseline members really were the population of the content pass:
        // a pass that examined nothing would also report complete coverage.
        XCTAssertEqual(
            firstRuns.first { $0.kind == .content }?.result?.examined,
            2
        )
        XCTAssertTrue(restarted.queue.allEntries().isEmpty)

        // And again once the content interval has actually come round, which is
        // the moment the gate names. A due interval reads the same bytes and
        // still finds nothing owed.
        clock.advance(FolderReconciliationKind.content.interval * 2)
        let due = try restarted.folderReconciliation.schedule()
        XCTAssertTrue(due.contains { $0.kind == .content && $0.trigger == .due })
        let dueRuns = await restarted.folderReconciliationExecutor.run(due)
        XCTAssertEqual(Set(dueRuns.compactMap(\.outcome)), [.complete])
        XCTAssertTrue(dueRuns.allSatisfy { !$0.drifted })
        XCTAssertEqual(dueRuns.first { $0.kind == .content }?.result?.examined, 2)
        XCTAssertTrue(restarted.queue.allEntries().isEmpty)
        let afterContent = await restarted.folderMonitoring.reconcileAll()
        XCTAssertEqual(afterContent.map(\.queued), [0])
        XCTAssertTrue(restarted.queue.allEntries().isEmpty)
        XCTAssertTrue(
            restarted.folderReconciliation
                .coverage(subscriptionID: established.subscriptionID)
                .allSatisfy { $0.isUpToDate(at: clock.now) }
        )
    }

    /// MON-30: a member whose last outcome was a known negative — a refusal and a
    /// zero-text extraction. Repeated events and both the 15-minute and the
    /// 24-hour check leave it alone, and none of them reports it as added.
    func testAKnownNegativeOutcomeIsNotReprocessedByRepeatedEventsOrEitherIntervalCheck() async throws {
        let root = try temporaryDirectory()
        let state = root.appendingPathComponent("state", isDirectory: true)
        let refused = root.appendingPathComponent("refused.txt")
        let blank = root.appendingPathComponent("blank.md")
        try Data("one".utf8).write(to: refused)
        try Data("two".utf8).write(to: blank)
        let clock = Clock(Date(timeIntervalSince1970: 1_760_000_000))
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec(),
            reconciliationClock: { clock.now }
        )
        let submission = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(mode: .keepUpdated, recursive: true, includeHidden: false)
        )
        let baseline = try await submission.submit(plan)
        for (index, entry) in baseline.enumerated() {
            try admit(
                entry,
                captureID: "dcap-" + String(repeating: String(format: "%x", index + 1), count: 64),
                queue: environment.queue
            )
        }
        _ = await environment.folderMonitoring.reconcileAll()
        let admitted = try XCTUnwrap(environment.folderSubscriptions.all().first)
        XCTAssertEqual(admitted.state, .active)
        let baselineCaptures = Set(admitted.members.compactMap(\.currentSuccessfulCaptureID))
        XCTAssertEqual(baselineCaptures.count, 2)
        // The control for the two interval claims below: while this watch is
        // ordinary, both kinds of periodic work are owed and armed. What silences
        // them later is the negative outcome, not an empty schedule.
        XCTAssertEqual(
            Set(try environment.folderReconciliation.schedule().map(\.kind)),
            Set(FolderReconciliationKind.allCases)
        )

        // A new version of each is read and queued, then Core reports a known
        // negative for both: one refused outright, one admitted no text.
        try Data("one changed".utf8).write(to: refused)
        try Data("two changed".utf8).write(to: blank)
        let changed = await environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(changed.map(\.queued), [2])
        let negatives = environment.queue.allEntries().filter {
            $0.intent.surface == .automaticFolder
                && !baseline.map(\.intent.itemId).contains($0.intent.itemId)
        }
        XCTAssertEqual(negatives.count, 2)
        for (entry, code) in zip(
            negatives.sorted { $0.intent.itemId < $1.intent.itemId },
            ["unsupported_format", "zero_text_extracted"]
        ) {
            _ = try environment.queue.markFailed(itemID: entry.intent.itemId, failureCode: code)
        }
        _ = await environment.folderMonitoring.reconcileReceipts()

        let remembered = try XCTUnwrap(environment.folderSubscriptions.all().first)
        XCTAssertEqual(remembered.state, .reviewRequired)
        // No success is shown for either refused version: neither digest is kept
        // as synchronized, and the only captures on record are the baselines'.
        XCTAssertTrue(remembered.members.allSatisfy { $0.lastObservedDigest == nil })
        XCTAssertTrue(remembered.members.allSatisfy { $0.lastQueuedItemID == nil })
        XCTAssertEqual(
            Set(remembered.members.compactMap(\.currentSuccessfulCaptureID)),
            baselineCaptures
        )

        // Repeated events change nothing: the known negative is remembered, so no
        // further read or publication is attempted for either member.
        let before = environment.queue.allEntries()
        for _ in 0..<3 {
            await environment.folderMonitoring.markDirty(
                subscriptionIDs: [remembered.subscriptionID]
            )
            let repeated = await environment.folderMonitoring.reconcileAll()
            XCTAssertEqual(repeated.map(\.queued), [0])
        }
        XCTAssertEqual(environment.queue.allEntries(), before)

        // And neither interval check revives it. Nothing is owed at 15 minutes or
        // at 24 hours, and a demand handed over anyway reads nothing.
        for interval in [
            FolderReconciliationKind.metadata.interval,
            FolderReconciliationKind.content.interval,
        ] {
            clock.advance(interval * 2)
            XCTAssertTrue(try environment.folderReconciliation.schedule().isEmpty)
            XCTAssertTrue(environment.folderReconciliation.due().isEmpty)
            let forced = await environment.folderReconciliationExecutor.run(
                FolderReconciliationKind.allCases.map {
                    FolderReconciliationScheduler.Demand(
                        subscriptionID: remembered.subscriptionID,
                        kind: $0,
                        trigger: .due
                    )
                }
            )
            XCTAssertEqual(forced.map(\.skipped), [.notActive, .notActive])
            XCTAssertTrue(forced.allSatisfy { $0.result == nil })
        }
        XCTAssertEqual(environment.queue.allEntries(), before)
        let negativeIDs = Set(negatives.map(\.intent.itemId))
        XCTAssertEqual(
            Set(
                environment.queue.allEntries()
                    .filter { negativeIDs.contains($0.intent.itemId) }
                    .compactMap(\.failureCode)
            ),
            ["unsupported_format", "zero_text_extracted"]
        )
    }

    /// MON-23: a disconnected source is not rediscovered. The folder owner's own
    /// release revokes only the route it owns; the source action revokes every
    /// local route, and a later scan over a tree that still holds the file reads
    /// nothing through it and never enrols it again.
    func testADisconnectedSourceIsNeverRediscoveredByALaterScan() async throws {
        let root = try temporaryDirectory()
        let kept = root.appendingPathComponent("kept.txt")
        let dropped = root.appendingPathComponent("dropped.txt")
        try Data("kept".utf8).write(to: kept)
        try Data("dropped".utf8).write(to: dropped)
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
        for (index, entry) in baseline.enumerated() {
            try admit(
                entry,
                captureID: "dcap-" + String(repeating: String(format: "%x", index + 1), count: 64),
                queue: environment.queue
            )
        }
        _ = await environment.folderMonitoring.reconcileAll()
        let watched = try XCTUnwrap(environment.folderSubscriptions.all().first)
        let member = try XCTUnwrap(watched.members.first { $0.displayName == "dropped.txt" })

        // Disconnect the source, not the folder: every route this Mac holds for
        // that one document is revoked and no other document is touched.
        let revoked = try environment.accessStore.disconnect(
            linkedDocumentID: member.linkedDocumentID
        )
        XCTAssertEqual(revoked.map(\.accessRef), [member.accessRef])
        XCTAssertTrue(
            environment.accessStore.descriptors()
                .filter { $0.accessRef != member.accessRef }
                .allSatisfy { $0.state == .connected }
        )

        // The file is still in the tree and its bytes change, which is exactly
        // what a scan would otherwise read and enrol.
        try Data("dropped and changed".utf8).write(to: dropped)
        try Data("kept and changed".utf8).write(to: kept)
        let rescan = await environment.folderMonitoring.reconcileAll()

        // The watch is unharmed and the other member is still read; the
        // disconnected one is neither read, nor queued, nor enrolled a second
        // time under a new membership.
        XCTAssertEqual(rescan.map(\.state), [.active])
        XCTAssertEqual(rescan.map(\.queued), [1])
        let after = try XCTUnwrap(environment.folderSubscriptions.all().first)
        XCTAssertEqual(after.members.count, watched.members.count)
        XCTAssertEqual(
            after.members.filter { $0.displayName == "dropped.txt" }.map(\.memberID),
            [member.memberID]
        )
        // Two baselines plus one new version of the connected file, and nothing
        // at all for the disconnected one.
        XCTAssertEqual(
            environment.queue.allEntries()
                .filter { $0.intent.surface == .automaticFolder }
                .compactMap(\.intent.displayName)
                .sorted(),
            ["dropped.txt", "kept.txt", "kept.txt"]
        )
        // Repeating the scan never catches up on it either.
        let again = await environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(again.map(\.queued), [0])
        XCTAssertThrowsError(
            try environment.accessStore.withReadAccess(
                accessRef: member.accessRef,
                expectedGeneration: member.membershipGeneration,
                expectedRootBindingGeneration: after.rootBindingGeneration
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .disconnected) }
    }

    /// MON-21: two genuinely overlapping watched roots and a direct route over the
    /// same file. The three routes are each their own — their own grant, membership
    /// and generation — but they name one canonical source and one linked document,
    /// and one capture of it serves all three. No identity anywhere is derived from
    /// the bytes, so byte-identical twins stay apart, and nothing is shared with
    /// another workspace.
    func testOverlappingRootsAndADirectRouteShareOneSourceAcrossThreeRoutes() async throws {
        let outer = try temporaryDirectory()
        let inner = outer.appendingPathComponent("inner", isDirectory: true)
        try FileManager.default.createDirectory(at: inner, withIntermediateDirectories: true)
        let shared = inner.appendingPathComponent("shared.txt")
        // Byte-identical twins: an identity derived from content could not tell
        // them apart.
        let twin = inner.appendingPathComponent("twin.txt")
        try Data("identical".utf8).write(to: shared)
        try Data("identical".utf8).write(to: twin)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: outer.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec()
        )

        let outerWatch = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        _ = try await outerWatch.submit(
            try outerWatch.plan(
                root: outer,
                policy: FolderCapturePolicy(mode: .keepUpdated, recursive: true, includeHidden: false)
            )
        )
        let innerWatch = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        _ = try await innerWatch.submit(
            try innerWatch.plan(
                root: inner,
                policy: FolderCapturePolicy(mode: .keepUpdated, recursive: true, includeHidden: false)
            )
        )
        // The same file again, this time through its own direct route.
        _ = try await ManualCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        ).submit([ManualCaptureCandidate(url: shared, keepUpdated: true)])

        // Two overlapping roots, each with its own grant, and one direct route.
        let subscriptions = environment.folderSubscriptions.all()
        XCTAssertEqual(subscriptions.count, 2)
        XCTAssertEqual(Set(subscriptions.map(\.rootRef)).count, 2)
        let memberships = subscriptions.flatMap { subscription in
            subscription.members.filter { $0.displayName.hasSuffix("shared.txt") }
        }
        XCTAssertEqual(memberships.count, 2)
        let direct = try XCTUnwrap(environment.fileSubscriptions.all().first)
        let routes = environment.accessStore.descriptors()
        // Every membership is its own connected route, and the direct one is the
        // only route that does not resolve through a root group.
        XCTAssertEqual(
            Set(memberships.map(\.accessRef) + [direct.accessRef]).count,
            3
        )
        XCTAssertEqual(
            Set(routes.filter { $0.route == .directFile && $0.state == .connected }
                .map(\.linkedDocumentID)),
            [direct.linkedDocumentID]
        )
        XCTAssertTrue(
            memberships.allSatisfy { membership in
                routes.contains {
                    $0.accessRef == membership.accessRef
                        && $0.state == .connected
                        && $0.route != .directFile
                }
            }
        )

        // The three routes over one file name one source and one document, and
        // no identity anywhere is derived from the file's bytes: the two
        // byte-identical twins are a different source again, and nothing
        // contains the digest of what it names.
        let digest = "sha256:" + SHA256.hash(data: try Data(contentsOf: shared))
            .map { String(format: "%02x", $0) }.joined()
        let identities = subscriptions.flatMap(\.members).map(\.requestedSourceIdentity)
            + [direct.requestedSourceIdentity]
        XCTAssertEqual(identities.count, 5)
        XCTAssertEqual(
            Set(memberships.map(\.requestedSourceIdentity) + [direct.requestedSourceIdentity])
                .count,
            1
        )
        XCTAssertEqual(
            Set(memberships.map(\.linkedDocumentID) + [direct.linkedDocumentID]).count,
            1
        )
        XCTAssertTrue(identities.allSatisfy { !$0.contains(digest.dropFirst(7)) })
        XCTAssertTrue(identities.allSatisfy { $0.hasPrefix("source-") })
        let twins = subscriptions.flatMap(\.members)
            .filter { $0.displayName.hasSuffix("twin.txt") }
        XCTAssertEqual(twins.count, 2)
        // Both memberships of the twin are the twin's one source — and it is
        // never the shared file's, though their bytes are the same.
        XCTAssertEqual(Set(twins.map(\.requestedSourceIdentity)).count, 1)
        XCTAssertTrue(
            Set(twins.map(\.requestedSourceIdentity))
                .isDisjoint(with: Set(memberships.map(\.requestedSourceIdentity)))
        )
        // Two files, so two canonical sources in this workspace and no more.
        XCTAssertEqual(environment.canonicalSources.all().count, 2)
        XCTAssertEqual(Set(environment.canonicalSources.all().map(\.workspaceRef)), ["workspace-1"])

        // One capture of one proven original, not three of the same bytes: the
        // first route to read it queued it, and the two that followed coalesced
        // onto that one capture. It names the shared source, and it is the
        // direct route's baseline as much as the folder routes'.
        let initial = environment.queue.allEntries().filter {
            $0.intent.displayName?.hasSuffix("shared.txt") == true
        }
        XCTAssertEqual(initial.count, 1)
        let serving = try XCTUnwrap(initial.first)
        XCTAssertEqual(
            serving.intent.requestedSourceIdentity,
            direct.requestedSourceIdentity
        )
        XCTAssertEqual(direct.baselineItemID, serving.intent.itemId)
        // Each route still binds to its own membership, so the one that queued
        // the capture is the only one named in it — the other two are routes to
        // the same source, not copies of this intent.
        XCTAssertTrue(
            memberships.map(\.memberID)
                .contains(try XCTUnwrap(serving.intent.originalBinding?.memberId))
        )

        // Admitting that one capture makes every route over the file current at
        // it: one initial capture answers all three. The twin's own capture is
        // admitted alongside it, because a watch is active only once every one
        // of its memberships has a confirmed baseline.
        let baselineCaptureID = "dcap-" + String(repeating: "a", count: 64)
        try admit(serving, captureID: baselineCaptureID, queue: environment.queue)
        for entry in environment.queue.allEntries() where entry.state == .pending {
            try admit(entry, captureID: "dcap-" + String(repeating: "b", count: 64), queue: environment.queue)
        }
        let activated = await environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(Set(activated.map(\.state)), [.active])
        XCTAssertEqual(
            Set(environment.folderSubscriptions.all().flatMap(\.members)
                .filter { $0.displayName.hasSuffix("shared.txt") }
                .compactMap(\.currentSuccessfulCaptureID)),
            [baselineCaptureID]
        )
        // Nothing was read a second time for an unchanged file.
        XCTAssertEqual(
            environment.queue.allEntries().filter {
                $0.intent.displayName?.hasSuffix("shared.txt") == true
            }.count,
            1
        )

        // Changed bytes are exactly one new revision of the one canonical
        // source, claimed against the head it now has — not one revision per
        // route racing the others for it. No recorded identity moves.
        try Data("rewritten entirely".utf8).write(to: shared)
        _ = await environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(
            Set(environment.folderSubscriptions.all().flatMap(\.members)
                .map(\.requestedSourceIdentity)),
            Set(subscriptions.flatMap(\.members).map(\.requestedSourceIdentity))
        )
        let revisions = environment.queue.allEntries().filter {
            $0.intent.requestedSourceIdentity == serving.intent.requestedSourceIdentity
                && $0.intent.itemId != serving.intent.itemId
        }
        XCTAssertEqual(revisions.count, 1)
        XCTAssertEqual(
            revisions.first?.intent.basePrecondition,
            .expected(baselineCaptureID)
        )
        // Both memberships are waiting on that single revision.
        XCTAssertEqual(
            Set(environment.folderSubscriptions.all().flatMap(\.members)
                .filter { $0.displayName.hasSuffix("shared.txt") }
                .compactMap(\.lastQueuedItemID)),
            [try XCTUnwrap(revisions.first?.intent.itemId)]
        )

        // The memberships are held independently: stopping the inner watch
        // releases only the route it owns, and the outer membership still reads
        // the same file.
        let innerSubscription = try XCTUnwrap(
            subscriptions.first { $0.rootDisplayName == "inner" }
        )
        _ = try await environment.folderMonitoring.stop(
            subscriptionID: innerSubscription.subscriptionID
        )
        let outerMembership = try XCTUnwrap(
            subscriptions.first { $0.rootDisplayName != "inner" }?
                .members.first { $0.displayName.hasSuffix("shared.txt") }
        )
        let outerRootGeneration = try XCTUnwrap(
            subscriptions.first { $0.rootDisplayName != "inner" }?.rootBindingGeneration
        )
        XCTAssertEqual(
            try environment.accessStore.withReadAccess(
                accessRef: outerMembership.accessRef,
                expectedGeneration: outerMembership.membershipGeneration,
                expectedRootBindingGeneration: outerRootGeneration
            ) { try String(contentsOf: $0, encoding: .utf8) },
            "rewritten entirely"
        )

        // Nothing is reused across workspaces: this source's scheduling unit is
        // its own minted identity under the one workspace it was configured for,
        // so no other workspace names the same unit and nothing about it comes
        // from the bytes.
        let key = FileMonitoringCoordinator.schedulingKey(for: direct)
        XCTAssertEqual(key.source.workspaceRef, "workspace-1")
        XCTAssertEqual(key.source.sourceRef, direct.requestedSourceIdentity)
        XCTAssertFalse(key.source.sourceRef.contains(digest.dropFirst(7)))
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

    /// MON-34: the add/change burst threshold is a hundred items inside five
    /// minutes, and both halves of that are load-bearing. Ninety-nine additions
    /// are read and published without review. One more inside the same window is
    /// the hundredth and pauses the scope. The same hundredth item once the
    /// window has run out is not a burst at all.
    func testAnAddBurstReachesReviewOnlyAtAHundredChangesInsideFiveMinutes() async throws {
        let root = try temporaryDirectory()
        let watched = try watchedFolder(in: root)
        try Data("baseline".utf8).write(to: watched.appendingPathComponent("baseline.txt"))
        let clock = Clock(Date(timeIntervalSince1970: 1_760_000_000))
        let watch = try await activeWatch(root: root, clock: clock)

        // One short of the threshold: no review, and every one of them published.
        try writeFiles(99, into: watched, prefix: "first")
        let belowThreshold = await watch.environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(belowThreshold.map(\.state), [.active])
        XCTAssertEqual(belowThreshold.map(\.queued), [99])
        var subscription = try XCTUnwrap(watch.environment.folderSubscriptions.all().first)
        XCTAssertNil(subscription.reviewChangeCount)
        XCTAssertEqual(subscription.changeBurstCount, 99)
        let firstWindow = try XCTUnwrap(subscription.changeBurstWindowStartedAt)

        // Past five minutes the window has run out, so the hundredth item is
        // counted as the first of a new one rather than as the top of the old.
        clock.advance(301)
        try Data("late".utf8).write(to: watched.appendingPathComponent("hundredth.txt"))
        let outsideTheWindow = await watch.environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(outsideTheWindow.map(\.state), [.active])
        XCTAssertEqual(outsideTheWindow.map(\.queued), [1])
        subscription = try XCTUnwrap(watch.environment.folderSubscriptions.all().first)
        XCTAssertEqual(subscription.changeBurstCount, 1)
        XCTAssertNotEqual(subscription.changeBurstWindowStartedAt, firstWindow)

        // Inside that new window the count accumulates across passes, and the
        // hundredth observation pauses the scope before anything else is read.
        clock.advance(60)
        try writeFiles(99, into: watched, prefix: "second")
        let crossed = await watch.environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(crossed.map(\.state), [.reviewRequired])
        XCTAssertEqual(crossed.map(\.queued), [0])
        XCTAssertEqual(watch.environment.queue.allEntries().count, 100)
        subscription = try XCTUnwrap(watch.environment.folderSubscriptions.all().first)
        XCTAssertEqual(subscription.changeBurstCount, 100)
        XCTAssertEqual(subscription.reviewChangeCount, 99)
        XCTAssertNotNil(subscription.pendingReviewSignature)
    }

    /// MON-34 and MON-37: an approved backfill is exact. It exempts the one
    /// reviewed inventory and nothing else, so a reviewed member arriving at a
    /// size nobody approved sends the scope back to review with refreshed counts
    /// instead of riding in on the earlier approval.
    func testAnApprovedBackfillExemptionCoversOnlyTheExactReviewedInventory() async throws {
        let root = try temporaryDirectory()
        let watched = try watchedFolder(in: root)
        let edited = watched.appendingPathComponent("edited.txt")
        try Data("baseline".utf8).write(to: edited)
        let clock = Clock(Date(timeIntervalSince1970: 1_760_000_000))
        let watch = try await activeWatch(root: root, clock: clock)

        // Ninety-nine additions and one edit are a hundred credible changes.
        try writeFiles(99, into: watched, prefix: "added")
        try Data("baseline rewritten once".utf8).write(to: edited)
        let paused = await watch.environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(paused.map(\.state), [.reviewRequired])
        let review = try XCTUnwrap(watch.environment.folderSubscriptions.all().first)
        XCTAssertEqual(review.reviewChangeCount, 100)
        let reviewedInventory = try XCTUnwrap(review.pendingReviewSignature)

        // The approval is recorded against exactly what was reviewed.
        _ = try await watch.environment.folderMonitoring.resume(subscriptionID: watch.subscriptionID)
        XCTAssertEqual(
            watch.environment.folderSubscriptions.all().first?.approvedReviewSignature,
            reviewedInventory
        )

        // One reviewed member is now a different size. That is a materially
        // changed review scope: it is refreshed and reviewed again, nothing is
        // published, and the approval it outran does not stay on record.
        try Data("baseline rewritten once more".utf8).write(to: edited)
        let refreshed = await watch.environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(refreshed.map(\.state), [.reviewRequired])
        XCTAssertEqual(refreshed.map(\.queued), [0])
        XCTAssertTrue(watch.environment.queue.allEntries().isEmpty)
        let second = try XCTUnwrap(watch.environment.folderSubscriptions.all().first)
        XCTAssertEqual(second.reviewChangeCount, 100)
        XCTAssertNil(second.approvedReviewSignature)
        XCTAssertNotEqual(second.pendingReviewSignature, reviewedInventory)

        // Approving what is actually on the screen now drains exactly that.
        _ = try await watch.environment.folderMonitoring.resume(subscriptionID: watch.subscriptionID)
        let approved = await watch.environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(approved.map(\.state), [.active])
        XCTAssertEqual(approved.map(\.queued), [100])
        XCTAssertEqual(watch.environment.queue.allEntries().count, 100)
        let drained = try XCTUnwrap(watch.environment.folderSubscriptions.all().first)
        XCTAssertNil(drained.approvedReviewSignature)
        XCTAssertNil(drained.pendingReviewSignature)
    }

    /// MON-34 and MON-37: an approval is spent by the pass that drains it. The
    /// hundred changes it published stay in the five-minute rate window, but a
    /// quiet pass after them has nothing to review and must not reopen the scope.
    func testAQuietPassAfterAnApprovedBackfillDoesNotReopenReview() async throws {
        let root = try temporaryDirectory()
        let watched = try watchedFolder(in: root)
        let edited = watched.appendingPathComponent("edited.txt")
        try Data("baseline".utf8).write(to: edited)
        let clock = Clock(Date(timeIntervalSince1970: 1_760_000_000))
        let watch = try await activeWatch(root: root, clock: clock)

        // The hundredth change is an edit: the queue holds a hundred at most.
        try writeFiles(99, into: watched, prefix: "added")
        try Data("baseline rewritten".utf8).write(to: edited)
        let paused = await watch.environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(paused.map(\.state), [.reviewRequired])
        _ = try await watch.environment.folderMonitoring.resume(subscriptionID: watch.subscriptionID)
        let drained = await watch.environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(drained.map(\.queued), [100])

        clock.advance(30)
        let quiet = await watch.environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(quiet.map(\.state), [.active])
        XCTAssertEqual(quiet.map(\.queued), [0])
        XCTAssertNil(watch.environment.folderSubscriptions.all().first?.reviewChangeCount)

        // And the backfill is spent, not remembered: one more file in the same
        // window is the first of a new count, not the hundred-and-first (the
        // queue is full, so it is counted but waits).
        try Data("later".utf8).write(to: watched.appendingPathComponent("later.txt"))
        let after = await watch.environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(after.map(\.state), [.active])
        XCTAssertEqual(watch.environment.folderSubscriptions.all().first?.changeBurstCount, 1)
    }

    /// MON-35: a disappearance needs both twenty items and a tenth of the
    /// inventory, and the tenth is of the inventory that was present. Either
    /// condition alone is an ordinary absence, recorded without review.
    func testADisappearanceNeedsBothTwentyItemsAndATenthOfThePresentInventory() async throws {
        let root = try temporaryDirectory()
        let watched = try watchedFolder(in: root)
        try writeFiles(300, into: watched, prefix: "member")
        let clock = Clock(Date(timeIntervalSince1970: 1_760_000_000))
        let watch = try await activeWatch(root: root, clock: clock)
        XCTAssertEqual(watch.environment.folderSubscriptions.all().first?.members.count, 300)

        // Twenty items without the tenth: ordinary absence.
        try remove(20, named: "member", from: watched, startingAt: 0)
        let twentyOnly = await watch.environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(twentyOnly.map(\.state), [.active])
        XCTAssertEqual(twentyOnly.map(\.absent), [20])
        var subscription = try XCTUnwrap(watch.environment.folderSubscriptions.all().first)
        XCTAssertNil(subscription.reviewChangeCount)
        XCTAssertEqual(subscription.members.filter { $0.state == .absent }.count, 20)

        // Twenty-eight is both twenty items and a tenth of the two hundred and
        // eighty still present — though not a tenth of the three hundred ever
        // enrolled, which is what makes the share one of the present inventory.
        try remove(28, named: "member", from: watched, startingAt: 20)
        let bothConditions = await watch.environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(bothConditions.map(\.state), [.reviewRequired])
        subscription = try XCTUnwrap(watch.environment.folderSubscriptions.all().first)
        XCTAssertEqual(subscription.reviewChangeCount, 28)
        XCTAssertEqual(subscription.members.filter { $0.state == .absent }.count, 20)

        // A tenth without twenty items: also ordinary absence. Fifteen of a
        // hundred and fifty is exactly a tenth and still one item short.
        let smallRoot = try temporaryDirectory()
        let small = try watchedFolder(in: smallRoot)
        try writeFiles(150, into: small, prefix: "member")
        let smallWatch = try await activeWatch(root: smallRoot, clock: clock)
        try remove(15, named: "member", from: small, startingAt: 0)
        let tenthOnly = await smallWatch.environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(tenthOnly.map(\.state), [.active])
        XCTAssertEqual(tenthOnly.map(\.absent), [15])
        XCTAssertNil(smallWatch.environment.folderSubscriptions.all().first?.reviewChangeCount)
    }

    /// MON-35: coverage comes first. A scan that could not read part of the
    /// folder, and a root that is not there at all, both leave every membership
    /// exactly as it stands — so neither a partial scan nor a remount failure can
    /// present itself as a bulk disappearance.
    func testAPartialScanOrAnUnavailableRootMarksNothingAbsent() async throws {
        let root = try temporaryDirectory()
        let watched = try watchedFolder(in: root)
        try writeFiles(100, into: watched, prefix: "member")
        let nested = watched.appendingPathComponent("nested", isDirectory: true)
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        try Data("nested".utf8).write(to: nested.appendingPathComponent("brief.pdf"))
        let clock = Clock(Date(timeIntervalSince1970: 1_760_000_000))
        let watch = try await activeWatch(root: root, clock: clock)
        XCTAssertEqual(watch.environment.folderSubscriptions.all().first?.members.count, 101)

        // Thirty of a hundred and one gone is well past both thresholds, so this
        // would be a reviewed bulk disappearance if the scan were complete.
        try remove(30, named: "member", from: watched, startingAt: 0)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o000],
            ofItemAtPath: nested.path
        )
        let partial = await watch.environment.folderMonitoring.reconcileAll()
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755],
            ofItemAtPath: nested.path
        )

        XCTAssertEqual(partial.map(\.state), [.needsAccess])
        XCTAssertEqual(partial.map(\.absent), [0])
        var subscription = try XCTUnwrap(watch.environment.folderSubscriptions.all().first)
        XCTAssertEqual(subscription.members.filter { $0.state == .absent }.count, 0)
        XCTAssertNil(subscription.reviewChangeCount)

        // The same again with no root at all, which is what a volume that did not
        // come back looks like from here.
        _ = try await watch.environment.folderMonitoring.resume(subscriptionID: watch.subscriptionID)
        try FileManager.default.moveItem(
            at: watched,
            to: root.appendingPathComponent("unmounted", isDirectory: true)
        )
        let unavailable = await watch.environment.folderMonitoring.reconcileAll()

        XCTAssertEqual(unavailable.map(\.state), [.needsAccess])
        XCTAssertEqual(unavailable.map(\.absent), [0])
        subscription = try XCTUnwrap(watch.environment.folderSubscriptions.all().first)
        XCTAssertEqual(subscription.members.filter { $0.state == .absent }.count, 0)
        XCTAssertNil(subscription.reviewChangeCount)
        XCTAssertTrue(watch.environment.queue.allEntries().isEmpty)
    }

    /// MON-37: what an approval is signed against. The reviewed inventory — each
    /// changed member at the size it was shown at, and each member shown as
    /// missing — under the subscription, configuration and consent the review was
    /// shown for. None of those is interchangeable, so an approval cannot be
    /// replayed onto another scope, a reconfigured one, or a later size.
    func testAReviewApprovalIsSignedAgainstItsInventoryConfigurationAndConsent() throws {
        let reviewed = Self.reviewedSubscription()
        let changed = MonitoredCandidate(
            url: URL(fileURLWithPath: "/watched/report.pdf"),
            relativeComponents: ["report.pdf"],
            displayName: "report.pdf",
            contentKind: .pdf,
            byteLength: 4096
        )
        let missing = Self.reviewedMember()
        let signature = FolderMonitoringCoordinator.reviewSignature(
            subscription: reviewed,
            changes: [changed],
            disappearances: [missing]
        )

        XCTAssertEqual(
            signature,
            FolderMonitoringCoordinator.reviewSignature(
                subscription: reviewed,
                changes: [changed],
                disappearances: [missing]
            ),
            "the same reviewed inventory under the same configuration signs the same way"
        )
        for other in [
            Self.reviewedSubscription(subscriptionID: "subscription-2"),
            Self.reviewedSubscription(configGeneration: 2),
            Self.reviewedSubscription(consentGeneration: 2),
        ] {
            XCTAssertNotEqual(
                signature,
                FolderMonitoringCoordinator.reviewSignature(
                    subscription: other,
                    changes: [changed],
                    disappearances: [missing]
                )
            )
        }
        let resized = MonitoredCandidate(
            url: changed.url,
            relativeComponents: changed.relativeComponents,
            displayName: changed.displayName,
            contentKind: changed.contentKind,
            byteLength: changed.byteLength + 1
        )
        XCTAssertNotEqual(
            signature,
            FolderMonitoringCoordinator.reviewSignature(
                subscription: reviewed,
                changes: [resized],
                disappearances: [missing]
            )
        )
        XCTAssertNotEqual(
            signature,
            FolderMonitoringCoordinator.reviewSignature(
                subscription: reviewed,
                changes: [changed],
                disappearances: []
            )
        )
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

    /// The watched root, kept beside the state store rather than over it, so a
    /// test may make it unreadable or move it away without touching the store.
    private func watchedFolder(in root: URL) throws -> URL {
        let watched = root.appendingPathComponent("watched", isDirectory: true)
        try FileManager.default.createDirectory(at: watched, withIntermediateDirectories: true)
        return watched
    }

    /// A watch in `keepUpdated` + `onlyNewAndChanged` mode, active from the start
    /// with every existing member recorded locally. A later scan's credible
    /// changes are therefore exactly what happened after this call.
    private func activeWatch(
        root: URL,
        clock: Clock
    ) async throws -> (environment: DocumentIntakeEnvironment, subscriptionID: String) {
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec(),
            reconciliationClock: { clock.now }
        )
        let submission = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        let plan = try submission.plan(
            root: root.appendingPathComponent("watched", isDirectory: true),
            policy: FolderCapturePolicy(
                mode: .keepUpdated,
                recursive: true,
                includeHidden: false,
                watchInitialContent: .onlyNewAndChanged
            )
        )
        let submitted = try await submission.submit(plan)
        XCTAssertTrue(submitted.isEmpty)
        let subscription = try XCTUnwrap(environment.folderSubscriptions.all().first)
        XCTAssertEqual(subscription.state, .active)
        return (environment, subscription.subscriptionID)
    }

    private func writeFiles(_ count: Int, into directory: URL, prefix: String) throws {
        for index in 0..<count {
            try Data("\(prefix) \(index)".utf8).write(
                to: directory.appendingPathComponent(
                    String(format: "%@-%03d.txt", prefix, index)
                )
            )
        }
    }

    private func remove(_ count: Int, named prefix: String, from directory: URL, startingAt first: Int) throws {
        for index in first..<(first + count) {
            try FileManager.default.removeItem(
                at: directory.appendingPathComponent(String(format: "%@-%03d.txt", prefix, index))
            )
        }
    }

    private static func reviewedSubscription(
        subscriptionID: String = "subscription-1",
        configGeneration: UInt64 = 1,
        consentGeneration: UInt64 = 1
    ) -> FolderSubscription {
        FolderSubscription(
            subscriptionID: subscriptionID,
            rootRef: "root-1",
            rootDisplayName: "Watched",
            targetRef: "target-1",
            workspaceRef: "workspace-1",
            targetBindingRevision: "binding-1",
            callerContextBinding: CallerContextBinding(
                clientContextRef: "client-1",
                principalRef: "principal-1",
                contextRevision: "context-1"
            ),
            recursive: true,
            includeHidden: false,
            configGeneration: configGeneration,
            consentGeneration: consentGeneration,
            consentRecordedAt: "2026-10-16T00:00:00Z",
            observationEpoch: "epoch-1",
            state: .active,
            needsFullReconciliation: false,
            lastCompletedMetadataScanAt: nil,
            pendingReviewSignature: nil,
            approvedReviewSignature: nil,
            reviewChangeCount: nil,
            reviewExamples: nil,
            members: []
        )
    }

    private static func reviewedMember() -> FolderSubscriptionMember {
        FolderSubscriptionMember(
            memberID: "member-1",
            accessRef: "access-1",
            linkedDocumentID: "doc-1",
            requestedSourceIdentity: "source-1",
            relativeComponents: ["gone.txt"],
            displayName: "gone.txt",
            contentKind: .text,
            membershipGeneration: 1,
            baselineItemID: nil,
            currentSuccessfulCaptureID: nil,
            lastObservedDigest: "sha256:" + String(repeating: "a", count: 64),
            lastObservedByteLength: 8,
            lastObservedAt: "2026-10-16T00:00:00Z",
            lastQueuedItemID: nil,
            dirtyEpoch: 1,
            state: .present
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
