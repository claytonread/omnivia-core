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

    /// The deterministic stand-in for the process-monotonic clock the scheduler measures its quiet
    /// period and maximum wait in. Read from the detached preparation task as well as the test, so
    /// it is locked.
    private final class TestClock: @unchecked Sendable {
        private let lock = NSLock()
        private var value: Int64 = 0

        var milliseconds: Int64 { lock.withLock { value } }
        func advance(_ delta: Int64) { lock.withLock { value += delta } }
        var reader: @Sendable () -> Int64 { { [self] in milliseconds } }
    }

    /// Counts the reads of the original, which is how a test says "no source read happened", and
    /// can be switched to failing to exercise the claim that is owed again.
    private final class CountingReader: CoordinatedOriginalReading, @unchecked Sendable {
        private let lock = NSLock()
        private var reads = 0
        private var failing = false

        var count: Int { lock.withLock { reads } }
        func fail() { lock.withLock { failing = true } }

        func readStableBytes(from url: URL, maximumBytes: UInt64) throws -> Data {
            let failing = lock.withLock {
                reads += 1
                return self.failing
            }
            if failing { throw CapturePreparationError.coordinatedReadFailed }
            return try SystemCoordinatedOriginalReader()
                .readStableBytes(from: url, maximumBytes: maximumBytes)
        }
    }

    func testIndividualWatchWaitsForAdmissionThenQueuesChangedVersionAgainstCurrentHead() async throws {
        let root = try temporaryDirectory()
        let state = root.appendingPathComponent("state", isDirectory: true)
        let original = root.appendingPathComponent("watched.txt")
        try Data("baseline".utf8).write(to: original)
        let clock = TestClock()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec(),
            monotonicClock: clock.reader
        )
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
        // Admission makes the subscription active, but no event has been signalled, so there is
        // nothing due and nothing is read.
        let active = await environment.fileMonitoring.reconcileAll()
        XCTAssertEqual(active.map(\.queued), [false])
        let subscriptionID = try XCTUnwrap(environment.fileSubscriptions.all().first?.subscriptionID)

        await environment.fileMonitoring.markDirty(subscriptionIDs: [subscriptionID])
        clock.advance(3_000)
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

    func testRejectedAutomaticVersionPausesAndHoldsTheRetryUntilItsIntentResolves() async throws {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("watched.txt")
        try Data("baseline".utf8).write(to: original)
        let clock = TestClock()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec(),
            monotonicClock: clock.reader
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
        let subscriptionID = try await publishOneChange(environment: environment, clock: clock)
        let rejected = try XCTUnwrap(
            environment.queue.allEntries().first { $0.intent.surface == .automaticFile }
        )
        _ = try environment.queue.markFailed(itemID: rejected.intent.itemId, failureCode: "policy_rejected")

        _ = await environment.fileMonitoring.reconcileReceipts()
        let paused = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(paused.state, .paused)
        XCTAssertNil(paused.lastObservedDigest)
        XCTAssertNil(paused.lastQueuedItemID)

        _ = try await environment.fileMonitoring.resume(subscriptionID: subscriptionID)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [subscriptionID])
        clock.advance(3_000)
        _ = await environment.fileMonitoring.reconcileAll()
        // The refused intent is still this scope's one published candidate, so the retry is owed
        // but not attempted: exactly one later dirty generation stands behind it. Resolving it
        // from the queue's terminal receipt is the next slice.
        let key = FileMonitoringCoordinator.schedulingKey(for: paused)
        let scheduled = try XCTUnwrap(environment.automaticScheduling.state(of: key))
        XCTAssertEqual(scheduled.activeGeneration, 1)
        XCTAssertTrue(scheduled.published)
        XCTAssertEqual(scheduled.intentRef, rejected.intent.itemId)
        XCTAssertEqual(scheduled.dirtyGeneration, 2)
        XCTAssertEqual(
            environment.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            1
        )
    }

    func testDuplicateIndividualAutomaticSourceIsRejectedBeforeAnotherCapture() async throws {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("watched.txt")
        try Data("baseline".utf8).write(to: original)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec()
        )
        let submission = ManualCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        _ = try await submission.submit([ManualCaptureCandidate(url: original, keepUpdated: true)])

        do {
            _ = try await submission.submit([ManualCaptureCandidate(url: original, keepUpdated: true)])
            XCTFail("duplicate automatic source should be rejected")
        } catch {
            XCTAssertEqual(error as? ManualCaptureSubmissionError, .duplicateAutomaticSource)
        }
        XCTAssertEqual(environment.queue.allEntries().count, 1)
        XCTAssertEqual(environment.fileSubscriptions.all().count, 1)
    }

    func testQuietPeriodHoldsTheCheckUntilSignalsStop() async throws {
        let context = try await activeWatch()
        try Data("changed".utf8).write(to: context.original)
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])

        context.clock.advance(2_999)
        let early = await context.environment.fileMonitoring.reconcileAll()
        XCTAssertEqual(early.map(\.queued), [false])
        XCTAssertEqual(context.reader.count, context.baselineReads)
        XCTAssertTrue(context.environment.queue.allEntries().allSatisfy { $0.intent.surface != .automaticFile })

        context.clock.advance(1)
        let due = await context.environment.fileMonitoring.reconcileAll()
        XCTAssertEqual(due.map(\.queued), [true])
    }

    func testMaximumWaitPublishesDespiteSignalsThatKeepArriving() async throws {
        let context = try await activeWatch()
        try Data("changed".utf8).write(to: context.original)
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])

        // A signal every two seconds never lets the quiet period elapse, so nothing is read until
        // the maximum wait is reached, measured from that first signal.
        for _ in 0..<14 {
            context.clock.advance(2_000)
            await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
            let held = await context.environment.fileMonitoring.reconcileAll()
            XCTAssertEqual(held.map(\.queued), [false])
            XCTAssertEqual(context.reader.count, context.baselineReads)
        }

        context.clock.advance(2_000)
        let due = await context.environment.fileMonitoring.reconcileAll()
        XCTAssertEqual(due.map(\.queued), [true])
        XCTAssertEqual(context.clock.milliseconds, 30_000)
    }

    func testUnchangedDigestCompletesTheClaimWithoutPublication() async throws {
        let context = try await activeWatch()
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(3_000)
        let reports = await context.environment.fileMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.queued), [false])
        XCTAssertEqual(context.reader.count, context.baselineReads + 1)
        XCTAssertTrue(context.environment.queue.allEntries().allSatisfy { $0.intent.surface != .automaticFile })
        // Terminal, not cancelled: the generation is resolved and the scope goes clean, which is
        // what stops being tracked at all.
        XCTAssertNil(context.environment.automaticScheduling.state(of: context.key))
        let value = try XCTUnwrap(context.environment.fileSubscriptions.all().first)
        XCTAssertFalse(value.needsReconciliation)
        XCTAssertNotNil(value.lastCheckedAt)
    }

    func testChangedBytesPublishAnIntentTheScopeThenHoldsByRef() async throws {
        let context = try await activeWatch()
        try Data("changed".utf8).write(to: context.original)
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(3_000)
        let reports = await context.environment.fileMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.queued), [true])
        let automatic = try XCTUnwrap(
            context.environment.queue.allEntries().first { $0.intent.surface == .automaticFile }
        )
        let scheduled = try XCTUnwrap(context.environment.automaticScheduling.state(of: context.key))
        XCTAssertEqual(scheduled.activeGeneration, 1)
        XCTAssertTrue(scheduled.published)
        XCTAssertEqual(scheduled.intentRef, automatic.intent.itemId)
        XCTAssertNil(scheduled.dirtyGeneration)
        XCTAssertEqual(
            context.environment.fileSubscriptions.all().first?.lastQueuedItemID,
            automatic.intent.itemId
        )
    }

    func testSignalsArrivingAfterAPublishedCheckBecomeOneDirtyObligation() async throws {
        let context = try await activeWatch()
        try Data("changed".utf8).write(to: context.original)
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(3_000)
        _ = await context.environment.fileMonitoring.reconcileAll()
        let readsAfterPublication = context.reader.count

        try Data("changed again".utf8).write(to: context.original)
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(1_000)
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(3_000)
        let reports = await context.environment.fileMonitoring.reconcileAll()

        // Two more events, one dirty record behind the candidate still in flight, and no second
        // read or publication while it is unresolved.
        XCTAssertEqual(reports.map(\.queued), [false])
        XCTAssertEqual(context.reader.count, readsAfterPublication)
        let scheduled = try XCTUnwrap(context.environment.automaticScheduling.state(of: context.key))
        XCTAssertEqual(scheduled.activeGeneration, 1)
        XCTAssertEqual(scheduled.dirtyGeneration, 3)
        XCTAssertEqual(
            context.environment.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            1
        )
    }

    func testPausedSubscriptionPublishesNothingAndKeepsItsObligation() async throws {
        let context = try await activeWatch()
        try Data("changed".utf8).write(to: context.original)
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        _ = try await context.environment.fileMonitoring.pause(subscriptionID: context.subscriptionID)
        context.clock.advance(3_000)
        let reports = await context.environment.fileMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.state), [.paused])
        XCTAssertEqual(context.reader.count, context.baselineReads)
        XCTAssertTrue(context.environment.queue.allEntries().allSatisfy { $0.intent.surface != .automaticFile })
        // A scope this call will not check is never claimed, so its one dirty obligation stands
        // untouched and Resume finds it owed rather than lost.
        let scheduled = try XCTUnwrap(context.environment.automaticScheduling.state(of: context.key))
        XCTAssertEqual(scheduled.dirtyGeneration, 1)
        XCTAssertNil(scheduled.activeGeneration)
    }

    func testFailedReadReturnsTheClaimToOneDirtyObligation() async throws {
        let context = try await activeWatch()
        try Data("changed".utf8).write(to: context.original)
        context.reader.fail()
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(3_000)
        let reports = await context.environment.fileMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.state), [.needsAccess])
        XCTAssertTrue(context.environment.queue.allEntries().allSatisfy { $0.intent.surface != .automaticFile })
        let scheduled = try XCTUnwrap(context.environment.automaticScheduling.state(of: context.key))
        XCTAssertEqual(scheduled.dirtyGeneration, 1)
        XCTAssertNil(scheduled.activeGeneration)

        // The obligation is as due as it was, but this call did not retry it and the next one
        // leaves it alone while the subscription needs access again.
        let attempted = context.reader.count
        _ = await context.environment.fileMonitoring.reconcileAll()
        XCTAssertEqual(context.reader.count, attempted)
        let again = try XCTUnwrap(context.environment.automaticScheduling.state(of: context.key))
        XCTAssertEqual(again.dirtyGeneration, 1)
        XCTAssertNil(again.activeGeneration)
    }

    func testDueWorkForAnotherScopeIsLeftAloneAndDoesNotDisplaceTheFileCheck() async throws {
        let context = try await activeWatch()
        // Due work no individual watch owns: another source's scope, at user priority, so an
        // unrestricted claim would take it ahead of the file and spend this call's claim on it.
        let unrelated = AutomaticSchedulingKey(
            source: AutomaticSchedulingSource(
                workspaceRef: "workspace-1",
                sourceRef: "source-elsewhere"
            ),
            scopeKey: "folder-root"
        )
        try context.environment.automaticScheduling.signal(
            unrelated,
            priority: .user,
            at: context.clock.milliseconds
        )
        try Data("changed".utf8).write(to: context.original)
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(3_000)

        let reports = await context.environment.fileMonitoring.reconcileAll()

        // The file's own scope was still read and published rather than passed over. One check
        // that publishes reads twice: once to compare the digest, once to stage the payload.
        XCTAssertEqual(reports.map(\.queued), [true])
        XCTAssertEqual(context.reader.count, context.baselineReads + 2)
        XCTAssertEqual(context.environment.automaticScheduling.state(of: context.key)?.published, true)
        // The other scope was never claimed, so it was never cancelled either: its dirty record,
        // generation, priority and signal times stand exactly as they were signalled.
        let untouched = try XCTUnwrap(context.environment.automaticScheduling.state(of: unrelated))
        XCTAssertEqual(untouched.dirtyGeneration, 1)
        XCTAssertNil(untouched.activeGeneration)
        XCTAssertFalse(untouched.published)
        XCTAssertEqual(untouched.priority, .user)
        XCTAssertEqual(untouched.firstSignalMilliseconds, 0)
        XCTAssertEqual(untouched.latestSignalMilliseconds, 0)
    }

    func testReconcilingOneSubscriptionLeavesAnotherSubscriptionsSchedulingAlone() async throws {
        let root = try temporaryDirectory()
        let paths = ["first.txt", "second.txt"].map { root.appendingPathComponent($0) }
        for path in paths { try Data("baseline".utf8).write(to: path) }
        let clock = TestClock()
        let reader = CountingReader()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec(),
            reader: reader,
            monotonicClock: clock.reader
        )
        let baseline = try await ManualCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        ).submit(paths.map { ManualCaptureCandidate(url: $0, keepUpdated: true) })
        for (index, entry) in baseline.enumerated() {
            try admit(
                entry,
                captureID: "dcap-" + String(repeating: "\(index)", count: 64),
                queue: environment.queue
            )
        }
        _ = await environment.fileMonitoring.reconcileReceipts()
        let watches = environment.fileSubscriptions.all()
        XCTAssertEqual(watches.map(\.state), [.active, .active])
        let baselineReads = reader.count

        // The subscription this call will not see has the obligation an unrestricted claim would
        // take first, and bytes to publish if anything ever read it.
        for path in paths { try Data("changed".utf8).write(to: path) }
        let excluded = FileMonitoringCoordinator.schedulingKey(for: watches[1])
        try environment.automaticScheduling.signal(excluded, priority: .user, at: 0)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [watches[0].subscriptionID])
        clock.advance(3_000)

        let reports = await environment.fileMonitoring.reconcile(
            subscriptionIDs: [watches[0].subscriptionID]
        )

        XCTAssertEqual(reports.map(\.subscriptionID), [watches[0].subscriptionID])
        XCTAssertEqual(reports.map(\.queued), [true])
        // The two reads of the one checked original — digest then payload — are the only reads
        // this call made, so the filtered-out subscription's original was never opened.
        XCTAssertEqual(reader.count, baselineReads + 2)
        XCTAssertEqual(
            environment.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            1
        )
        let untouched = try XCTUnwrap(environment.automaticScheduling.state(of: excluded))
        XCTAssertEqual(untouched.dirtyGeneration, 1)
        XCTAssertNil(untouched.activeGeneration)
        XCTAssertFalse(untouched.published)
        XCTAssertEqual(untouched.priority, .user)
        XCTAssertEqual(
            environment.fileSubscriptions.all().first { $0.subscriptionID == watches[1].subscriptionID },
            watches[1]
        )
    }

    // MARK: - Support

    private struct WatchContext {
        let environment: DocumentIntakeEnvironment
        let clock: TestClock
        let reader: CountingReader
        let original: URL
        let subscriptionID: String
        let key: AutomaticSchedulingKey
        /// Reads the baseline submission already spent, so a test can say "nothing was read".
        let baselineReads: Int
    }

    /// One admitted baseline watch on an unchanged original, with nothing signalled yet.
    private func activeWatch() async throws -> WatchContext {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("watched.txt")
        try Data("baseline".utf8).write(to: original)
        let clock = TestClock()
        let reader = CountingReader()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec(),
            reader: reader,
            monotonicClock: clock.reader
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
        _ = await environment.fileMonitoring.reconcileReceipts()
        let value = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(value.state, .active)
        return WatchContext(
            environment: environment,
            clock: clock,
            reader: reader,
            original: original,
            subscriptionID: value.subscriptionID,
            key: FileMonitoringCoordinator.schedulingKey(for: value),
            baselineReads: reader.count
        )
    }

    /// Signals one change and lets the scheduler's quiet period elapse, returning the subscription
    /// that published.
    private func publishOneChange(
        environment: DocumentIntakeEnvironment,
        clock: TestClock
    ) async throws -> String {
        _ = await environment.fileMonitoring.reconcileAll()
        let subscriptionID = try XCTUnwrap(environment.fileSubscriptions.all().first?.subscriptionID)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [subscriptionID])
        clock.advance(3_000)
        _ = await environment.fileMonitoring.reconcileAll()
        return subscriptionID
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
