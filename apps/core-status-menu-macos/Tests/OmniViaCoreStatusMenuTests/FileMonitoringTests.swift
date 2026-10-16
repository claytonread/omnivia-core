@testable import DocumentIntakeMacOS
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

    /// Core's statement about the capture behind unchanged bytes, scripted. An unchanged file is
    /// only "checked" on such a statement (design §8.5), so the tests that mean to reach that
    /// outcome say that Core stands behind it.
    private struct StandingCapture: CurrentCaptureVerifying {
        func verify(documentID: String, captureID: String?, observedDigest: String) -> CurrentCaptureVerdict {
            .current
        }
    }

    /// Counts the reads of the original, which is how a test says "no source read happened", and
    /// can be switched to failing to exercise the claim that is owed again.
    private final class CountingReader: CoordinatedOriginalReading, @unchecked Sendable {
        private let lock = NSLock()
        private var reads = 0
        private var failing = false
        private var during: (@Sendable () -> Void)?

        var count: Int { lock.withLock { reads } }
        func fail() { lock.withLock { failing = true } }

        /// Runs once, inside the next read of the original. `withReadAccess` resolves the bookmark
        /// under the publication gate and then releases it for the read itself, so this is exactly
        /// the window a disconnect, a relink or a pause lands in while a check is reading.
        func interfere(_ body: @escaping @Sendable () -> Void) { lock.withLock { during = body } }

        func readStableBytes(from url: URL, maximumBytes: UInt64) throws -> Data {
            let (failing, during) = lock.withLock { () -> (Bool, (@Sendable () -> Void)?) in
                reads += 1
                let pending = self.during
                self.during = nil
                return (self.failing, pending)
            }
            if failing { throw CapturePreparationError.coordinatedReadFailed }
            let bytes = try SystemCoordinatedOriginalReader()
                .readStableBytes(from: url, maximumBytes: maximumBytes)
            during?()
            return bytes
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

    /// END-01: an interrupted initial add keeps its identities. The watch is still waiting for
    /// Core across a restart, nothing is read or observed until Core's admission is confirmed, and
    /// the confirmation then activates the very subscription the add created.
    func testAWaitingFileWatchKeepsItsIdentitiesAcrossARestartAndReadsNothingUntilActivated() async throws {
        let root = try temporaryDirectory()
        let state = root.appendingPathComponent("state", isDirectory: true)
        let original = root.appendingPathComponent("watched.txt")
        try Data("baseline".utf8).write(to: original)
        let reader = CountingReader()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec(),
            reader: reader
        )
        let baseline = try await ManualCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        ).submit([ManualCaptureCandidate(url: original, keepUpdated: true)])
        let submitted = try XCTUnwrap(environment.fileSubscriptions.all().first)
        let readsForTheBaseline = reader.count
        XCTAssertEqual(submitted.state, .waitingForCore)
        try Data("changed before activation".utf8).write(to: original)

        let reopenedReader = CountingReader()
        let reopened = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec(),
            reader: reopenedReader
        )
        let restored = try XCTUnwrap(reopened.fileSubscriptions.all().first)
        XCTAssertEqual(reopened.fileSubscriptions.all().count, 1)
        XCTAssertEqual(restored, submitted)
        XCTAssertEqual(reopened.queue.allEntries().map(\.intent.itemId), baseline.map(\.intent.itemId))
        XCTAssertEqual(restored.baselineItemID, baseline[0].intent.itemId)

        await reopened.fileMonitoring.markDirty(subscriptionIDs: [restored.subscriptionID])
        let waiting = await reopened.fileMonitoring.reconcileAll()
        XCTAssertEqual(waiting.map(\.state), [.waitingForCore])
        XCTAssertEqual(waiting.map(\.queued), [false])
        let beforeActivation = await reopened.fileMonitoring.activeObservationRoots()
        XCTAssertTrue(beforeActivation.isEmpty)
        XCTAssertEqual(reopenedReader.count, 0)
        XCTAssertEqual(readsForTheBaseline, 1)
        XCTAssertEqual(reopened.queue.allEntries().count, 1)

        let captureID = "dcap-" + String(repeating: "a", count: 64)
        try admit(baseline[0], captureID: captureID, queue: reopened.queue)
        let active = await reopened.fileMonitoring.reconcileAll()

        XCTAssertEqual(active.map(\.state), [.active])
        let activated = try XCTUnwrap(reopened.fileSubscriptions.all().first)
        XCTAssertEqual(activated.subscriptionID, submitted.subscriptionID)
        XCTAssertEqual(activated.accessRef, submitted.accessRef)
        XCTAssertEqual(activated.baselineItemID, submitted.baselineItemID)
        XCTAssertEqual(activated.currentSuccessfulCaptureID, captureID)
        let afterActivation = await reopened.fileMonitoring.activeObservationRoots()
        XCTAssertEqual(afterActivation.map(\.subscriptionID), [submitted.subscriptionID])
    }

    /// END-01: stopping a file watch whose initial capture Core has not yet answered is a one-way
    /// door, whichever way the late answer goes. An admission is not recorded against the stopped
    /// watch, and a refusal does not send it to `.paused`, which Resume accepts.
    func testALateReceiptDoesNotReactivateALocallyStoppedFileWatch() async throws {
        for outcome in ["admitted", "failed"] {
            let root = try temporaryDirectory()
            let state = root.appendingPathComponent("state", isDirectory: true)
            let original = root.appendingPathComponent("watched.txt")
            try Data("baseline".utf8).write(to: original)
            let environment = try DocumentIntakeEnvironment(stateRoot: state, bookmarkCodec: BookmarkCodec())
            let baseline = try await ManualCaptureSubmission(
                environment: environment,
                workspaceRef: "workspace-1"
            ).submit([ManualCaptureCandidate(url: original, keepUpdated: true)])
            let subscriptionID = try XCTUnwrap(environment.fileSubscriptions.all().first?.subscriptionID)
            XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.waitingForCore])

            _ = try await environment.fileMonitoring.stop(subscriptionID: subscriptionID)
            if outcome == "admitted" {
                try admit(
                    baseline[0],
                    captureID: "dcap-" + String(repeating: "a", count: 64),
                    queue: environment.queue
                )
            } else {
                _ = try environment.queue.markFailed(
                    itemID: baseline[0].intent.itemId,
                    failureCode: "delivery-refused"
                )
            }

            _ = await environment.fileMonitoring.reconcileReceipts()
            let reports = await environment.fileMonitoring.reconcileAll()
            XCTAssertEqual(reports.map(\.state), [.stopped], outcome)

            try await assertStoppedForGood(environment, subscriptionID: subscriptionID, outcome: outcome)
            // And the stop survives a restart.
            let reopened = try DocumentIntakeEnvironment(stateRoot: state, bookmarkCodec: BookmarkCodec())
            _ = await reopened.fileMonitoring.reconcileReceipts()
            try await assertStoppedForGood(reopened, subscriptionID: subscriptionID, outcome: outcome)
        }
    }

    private func assertStoppedForGood(
        _ environment: DocumentIntakeEnvironment,
        subscriptionID: String,
        outcome: String
    ) async throws {
        let stopped = try XCTUnwrap(
            environment.fileSubscriptions.all().first { $0.subscriptionID == subscriptionID },
            outcome
        )
        XCTAssertEqual(stopped.state, .stopped, outcome)
        XCTAssertNil(stopped.currentSuccessfulCaptureID, outcome)
        XCTAssertFalse(stopped.needsReconciliation, outcome)
        let observed = await environment.fileMonitoring.activeObservationRoots()
        XCTAssertTrue(observed.isEmpty, outcome)
        do {
            _ = try await environment.fileMonitoring.resume(subscriptionID: subscriptionID)
            XCTFail("a stopped watch must not resume: \(outcome)")
        } catch {
            XCTAssertEqual(error as? FolderMonitoringError, .subscriptionNotActive, outcome)
        }
    }

    func testRejectedAutomaticVersionPausesAndCompletesTheGenerationItPublished() async throws {
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
        XCTAssertTrue(paused.needsReconciliation)
        // The refusal is the terminal outcome of this scope's one published intent, so the
        // generation that published it is finished and nothing is left in flight: the scope goes
        // clean, which is what stops it being tracked at all.
        let key = FileMonitoringCoordinator.schedulingKey(for: paused)
        XCTAssertNil(environment.automaticScheduling.state(of: key))

        _ = try await environment.fileMonitoring.resume(subscriptionID: subscriptionID)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [subscriptionID])
        clock.advance(3_000)
        let reports = await environment.fileMonitoring.reconcileAll()

        // The cleared digest is what makes Resume deliberately prepare a new intent instead of
        // treating the refused version as synchronized, and exactly one is published: the refused
        // one is terminal and is never published again.
        XCTAssertEqual(reports.map(\.queued), [true])
        let automatic = environment.queue.allEntries().filter { $0.intent.surface == .automaticFile }
        XCTAssertEqual(automatic.count, 2)
        XCTAssertEqual(automatic.filter { $0.state == .failed }.map(\.intent.itemId), [rejected.intent.itemId])
        let scheduled = try XCTUnwrap(environment.automaticScheduling.state(of: key))
        XCTAssertTrue(scheduled.published)
        XCTAssertNotEqual(scheduled.intentRef, rejected.intent.itemId)
        XCTAssertNil(scheduled.dirtyGeneration)
    }

    func testAdmittedAutomaticReceiptAppliesTheCaptureAndCompletesTheGeneration() async throws {
        let context = try await activeWatch()
        let published = try await publishOneAutomaticChange(context)
        let readsAfterPublication = context.reader.count
        let captureID = "dcap-" + String(repeating: "c", count: 64)
        try admit(published, captureID: captureID, queue: context.environment.queue)

        let reports = await context.environment.fileMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.state), [.active])
        XCTAssertEqual(reports.map(\.queued), [false])
        let value = try XCTUnwrap(context.environment.fileSubscriptions.all().first)
        XCTAssertEqual(value.currentSuccessfulCaptureID, captureID)
        XCTAssertNil(value.lastQueuedItemID)
        // Terminal: the generation that published is resolved and dropped, so the scope is clean
        // and no longer tracked. Nothing was read and nothing was published to get there.
        XCTAssertNil(context.environment.automaticScheduling.state(of: context.key))
        XCTAssertEqual(context.reader.count, readsAfterPublication)
        XCTAssertEqual(
            context.environment.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            1
        )
    }

    func testReconcilingReceiptsAloneCompletesTheGenerationAndExposesTheLaterOne() async throws {
        let context = try await activeWatch()
        let published = try await publishOneAutomaticChange(context)
        // One more event arrives behind the candidate in flight, so there is a later generation to
        // survive the completion.
        try Data("changed again".utf8).write(to: context.original)
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        let readsAfterPublication = context.reader.count
        try admit(
            published,
            captureID: "dcap-" + String(repeating: "c", count: 64),
            queue: context.environment.queue
        )

        _ = await context.environment.fileMonitoring.reconcileReceipts()

        // This entry point claims nothing and reads nothing, but it still finished the published
        // generation, which is what leaves generation 2 owed, unclaimed and unpublished.
        let resolved = try XCTUnwrap(context.environment.automaticScheduling.state(of: context.key))
        XCTAssertNil(resolved.activeGeneration)
        XCTAssertFalse(resolved.published)
        XCTAssertNil(resolved.intentRef)
        XCTAssertEqual(resolved.dirtyGeneration, 2)
        XCTAssertEqual(context.reader.count, readsAfterPublication)
        XCTAssertEqual(
            context.environment.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            1
        )

        context.clock.advance(3_000)
        let reports = await context.environment.fileMonitoring.reconcileAll()

        // Generation 2 is the next owed generation and publishes exactly one more intent: the
        // resolved generation is not published a second time.
        XCTAssertEqual(reports.map(\.queued), [true])
        let scheduled = try XCTUnwrap(context.environment.automaticScheduling.state(of: context.key))
        XCTAssertEqual(scheduled.activeGeneration, 2)
        XCTAssertTrue(scheduled.published)
        XCTAssertNil(scheduled.dirtyGeneration)
        XCTAssertNotEqual(scheduled.intentRef, published.intent.itemId)
        XCTAssertEqual(
            context.environment.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            2
        )
    }

    func testRefusedCompletionKeepsTheScopePublishedUntilAReplayCompletesIt() async throws {
        let context = try await activeWatch()
        let published = try await publishOneAutomaticChange(context)
        let readsAfterPublication = context.reader.count
        let captureID = "dcap-" + String(repeating: "c", count: 64)
        try admit(published, captureID: captureID, queue: context.environment.queue)
        // The receipt is recorded before the completion, so an unwritable scheduling document is
        // the one window the two durable writes can be split across.
        let scheduling = context.stateRoot
            .appendingPathComponent("automatic-scheduling", isDirectory: true)
        try setPermissions(0o500, of: scheduling)
        let schedulingPath = scheduling.path
        addTeardownBlock {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o700], ofItemAtPath: schedulingPath
            )
        }

        let refused = await context.environment.fileMonitoring.reconcileAll()

        // The receipt effects stand; the completion does not. Reporting this as resolved would let
        // the next claim publish a second intent for a generation that is still published, so the
        // scope stays non-checkable: nothing was read and nothing was published behind it.
        XCTAssertEqual(refused.map(\.state), [.active])
        XCTAssertEqual(refused.map(\.queued), [false])
        XCTAssertEqual(context.reader.count, readsAfterPublication)
        let applied = try XCTUnwrap(context.environment.fileSubscriptions.all().first)
        XCTAssertEqual(applied.currentSuccessfulCaptureID, captureID)
        XCTAssertNil(applied.lastQueuedItemID)
        let held = try XCTUnwrap(context.environment.automaticScheduling.state(of: context.key))
        XCTAssertEqual(held.activeGeneration, 1)
        XCTAssertTrue(held.published)
        XCTAssertEqual(held.intentRef, published.intent.itemId)

        try setPermissions(0o700, of: scheduling)
        _ = await context.environment.fileMonitoring.reconcileReceipts()

        // The replay resolves the same still-published ref against the same terminal entry, finds
        // the receipt effects already applied, and completes it then. Nothing is applied twice.
        XCTAssertNil(context.environment.automaticScheduling.state(of: context.key))
        XCTAssertEqual(context.environment.fileSubscriptions.all().first, applied)
        XCTAssertEqual(
            context.environment.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            1
        )
    }

    func testRestartResolvesTheTerminalReceiptThroughTheRecoveredPublicationRef() async throws {
        let context = try await activeWatch()
        let published = try await publishOneAutomaticChange(context)
        let captureID = "dcap-" + String(repeating: "c", count: 64)
        try admit(published, captureID: captureID, queue: context.environment.queue)

        // A new process: the publication, its ref and the terminal entry all come off disk.
        let reader = CountingReader()
        let reopened = try DocumentIntakeEnvironment(
            stateRoot: context.stateRoot,
            bookmarkCodec: BookmarkCodec(),
            reader: reader,
            monotonicClock: TestClock().reader
        )
        let recovered = try XCTUnwrap(reopened.automaticScheduling.state(of: context.key))
        XCTAssertTrue(recovered.published)
        XCTAssertEqual(recovered.intentRef, published.intent.itemId)

        _ = await reopened.fileMonitoring.reconcileReceipts()

        XCTAssertNil(reopened.automaticScheduling.state(of: context.key))
        let value = try XCTUnwrap(reopened.fileSubscriptions.all().first)
        XCTAssertEqual(value.currentSuccessfulCaptureID, captureID)
        XCTAssertEqual(reader.count, 0)

        // And once more, on a third process: there is nothing left to resolve, so nothing is
        // completed, applied or published again.
        let again = try DocumentIntakeEnvironment(
            stateRoot: context.stateRoot,
            bookmarkCodec: BookmarkCodec(),
            reader: reader,
            monotonicClock: TestClock().reader
        )
        _ = await again.fileMonitoring.reconcileReceipts()
        XCTAssertNil(again.automaticScheduling.state(of: context.key))
        XCTAssertEqual(again.fileSubscriptions.all().first, value)
        XCTAssertEqual(reader.count, 0)
        XCTAssertEqual(
            again.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
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

    func testUnchangedBytesRecordNoCheckWhenTheAccessIsDisconnectedDuringTheRead() async throws {
        let context = try await activeWatch()
        let before = try XCTUnwrap(context.environment.fileSubscriptions.all().first)
        let store = context.environment.accessStore
        let accessRef = before.accessRef
        context.reader.interfere { _ = try? store.disconnect(accessRef: accessRef) }

        let reports = try await checkOnce(context)

        // The bytes are the ones this scope already has, but the grant that authorised reading
        // them was withdrawn inside the read, so there is nothing to report as up to date.
        XCTAssertEqual(reports.map(\.queued), [false])
        XCTAssertEqual(context.reader.count, context.baselineReads + 1)
        try assertNothingWasRecorded(context, before: before)
        XCTAssertEqual(context.environment.fileSubscriptions.all().map(\.state), [.active])
    }

    func testUnchangedBytesRecordNoCheckWhenTheBindingGenerationMovesDuringTheRead() async throws {
        let context = try await activeWatch()
        let before = try XCTUnwrap(context.environment.fileSubscriptions.all().first)
        let store = context.environment.accessStore
        let accessRef = before.accessRef
        let documentID = before.linkedDocumentID
        let original = context.original
        // A relink leaves a perfectly usable grant behind. It is just not the one this read was
        // authorised under, so the check it would finish was never made under authority it holds.
        context.reader.interfere {
            _ = try? store.disconnect(accessRef: accessRef)
            _ = try? store.adopt(
                accessRef: accessRef,
                linkedDocumentID: documentID,
                selectedURL: original,
                route: .directFile
            )
        }

        let reports = try await checkOnce(context)

        XCTAssertEqual(reports.map(\.queued), [false])
        XCTAssertEqual(store.descriptors().first?.generation, before.bindingGeneration + 1)
        try assertNothingWasRecorded(context, before: before)
    }

    func testUnchangedBytesRecordNoCheckWhenTheWatchIsPausedDuringTheRead() async throws {
        let context = try await activeWatch()
        let before = try XCTUnwrap(context.environment.fileSubscriptions.all().first)
        let subscriptions = context.environment.fileSubscriptions
        let subscriptionID = context.subscriptionID
        // The same two writes `pause` makes, landing while the read this check was claimed for is
        // still in flight.
        context.reader.interfere {
            _ = try? subscriptions.update(id: subscriptionID) {
                $0.state = .paused
                $0.needsReconciliation = true
            }
        }

        let reports = try await checkOnce(context)

        XCTAssertEqual(reports.map(\.queued), [false])
        // A pause stays a pause: the refusal records nothing of its own over whoever moved it.
        XCTAssertEqual(reports.map(\.state), [.paused])
        try assertNothingWasRecorded(context, before: before)
        XCTAssertEqual(context.environment.fileSubscriptions.all().map(\.state), [.paused])
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

    func testSplitPublicationIsRepairedFromTheEntryTheQueueAlreadyCommitted() async throws {
        let context = try await activeWatch()
        try Data("changed".utf8).write(to: context.original)
        // The queue commit stands and the scheduling write that should have followed it does not,
        // which is the one window the two durable writes of a publication can be split across.
        await context.environment.fileMonitoring.failNextSchedulingPublicationForTesting()
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(3_000)

        let reports = await context.environment.fileMonitoring.reconcileAll()

        // One check and one intent: the entry publication committed is recorded, not replaced. A
        // publishing check reads the original twice — once for its digest and once to prepare the
        // payload — so the repair costing nothing extra is what this count says.
        XCTAssertEqual(reports.map(\.queued), [true])
        XCTAssertEqual(reports.map(\.state), [.active])
        XCTAssertEqual(context.reader.count, context.baselineReads + 2)
        let automatic = context.environment.queue.allEntries().filter {
            $0.intent.surface == .automaticFile
        }
        XCTAssertEqual(automatic.count, 1)
        // The claim was never cancelled: the same generation is published, by the committed ref.
        let scheduled = try XCTUnwrap(context.environment.automaticScheduling.state(of: context.key))
        XCTAssertEqual(scheduled.activeGeneration, 1)
        XCTAssertTrue(scheduled.published)
        XCTAssertEqual(scheduled.intentRef, automatic[0].intent.itemId)
        XCTAssertNil(scheduled.dirtyGeneration)
        let value = try XCTUnwrap(context.environment.fileSubscriptions.all().first)
        XCTAssertEqual(value.dirtyEpoch, 2)
        XCTAssertEqual(value.lastQueuedItemID, automatic[0].intent.itemId)
        XCTAssertEqual(value.lastObservedDigest, automatic[0].intent.localIntegrityDigest)
        XCTAssertEqual(value.lastObservedByteLength, automatic[0].intent.byteLength)
        XCTAssertNotNil(value.lastObservedAt)
        XCTAssertNotNil(value.lastCheckedAt)
        XCTAssertFalse(value.needsReconciliation)

        // And the repaired record is what the next reconciliation refuses a duplicate on.
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(3_000)
        let again = await context.environment.fileMonitoring.reconcileAll()

        XCTAssertEqual(again.map(\.queued), [false])
        XCTAssertEqual(context.reader.count, context.baselineReads + 2)
        XCTAssertEqual(
            context.environment.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            1
        )
        let held = try XCTUnwrap(context.environment.automaticScheduling.state(of: context.key))
        XCTAssertEqual(held.activeGeneration, 1)
        XCTAssertEqual(held.dirtyGeneration, 2)
        XCTAssertEqual(held.intentRef, automatic[0].intent.itemId)
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

    func testPublishedIntentBlocksTheNextCheckByItsOwnRef() async throws {
        let context = try await activeWatch()
        try Data("changed".utf8).write(to: context.original)
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(3_000)
        _ = await context.environment.fileMonitoring.reconcileAll()
        let published = try XCTUnwrap(
            context.environment.queue.allEntries().first { $0.intent.surface == .automaticFile }
        )
        // The subscription's own note of what it queued is gone, so the durable ref is the only
        // thing that can still name this scope's one unresolved intent.
        _ = try context.environment.fileSubscriptions.update(id: context.subscriptionID) {
            $0.lastQueuedItemID = nil
        }
        let readsAfterPublication = context.reader.count

        try Data("changed again".utf8).write(to: context.original)
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(3_000)
        let reports = await context.environment.fileMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.state), [.active])
        XCTAssertEqual(reports.map(\.queued), [false])
        XCTAssertEqual(context.reader.count, readsAfterPublication)
        XCTAssertEqual(
            context.environment.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            1
        )
        let scheduled = try XCTUnwrap(context.environment.automaticScheduling.state(of: context.key))
        XCTAssertEqual(scheduled.intentRef, published.intent.itemId)
        XCTAssertEqual(scheduled.dirtyGeneration, 2)
    }

    func testPublishedIntentThatIsNotInTheQueueSendsTheSubscriptionToReview() async throws {
        let context = try await activeWatch()
        let scheduling = context.environment.automaticScheduling
        try scheduling.signal(context.key, priority: .background, at: context.clock.milliseconds)
        context.clock.advance(3_000)
        let claim = try XCTUnwrap(
            scheduling.claim(at: context.clock.milliseconds, among: [context.key])
        )
        try scheduling.markPublished(
            context.key,
            generation: claim.generation,
            intentRef: "item-vanished",
            at: context.clock.milliseconds
        )
        try Data("changed".utf8).write(to: context.original)

        let reports = await context.environment.fileMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.state), [.reviewRequired])
        XCTAssertEqual(context.environment.fileSubscriptions.all().map(\.state), [.reviewRequired])
        XCTAssertEqual(context.reader.count, context.baselineReads)
        XCTAssertTrue(context.environment.queue.allEntries().allSatisfy { $0.intent.surface != .automaticFile })
        // Nothing of the unaccountable publication is rewritten: resolving it is a decision this
        // owner cannot make, so it is left exactly as it stands for review.
        let scheduled = try XCTUnwrap(scheduling.state(of: context.key))
        XCTAssertEqual(scheduled.activeGeneration, 1)
        XCTAssertTrue(scheduled.published)
        XCTAssertEqual(scheduled.intentRef, "item-vanished")
    }

    func testPublishedIntentBelongingToAnotherSubscriptionSendsThisOneToReview() async throws {
        let root = try temporaryDirectory()
        let theirOriginal = root.appendingPathComponent("first.txt")
        let myOriginal = root.appendingPathComponent("second.txt")
        let paths = [theirOriginal, myOriginal]
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
        // The store orders by subscription ID, which is a fresh UUID, so neither watch can be
        // taken by position: each is identified by the original it was registered for.
        let watches = environment.fileSubscriptions.all()
        XCTAssertEqual(watches.map(\.state), [.active, .active])
        let theirWatch = try XCTUnwrap(
            watches.first { $0.displayName == theirOriginal.lastPathComponent }
        )
        let myWatch = try XCTUnwrap(
            watches.first { $0.displayName == myOriginal.lastPathComponent }
        )
        XCTAssertNotEqual(theirWatch.subscriptionID, myWatch.subscriptionID)

        try Data("changed".utf8).write(to: theirOriginal)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [theirWatch.subscriptionID])
        clock.advance(3_000)
        _ = await environment.fileMonitoring.reconcileAll()
        let theirs = try XCTUnwrap(
            environment.queue.allEntries().first { $0.intent.surface == .automaticFile }
        )
        XCTAssertEqual(theirs.intent.consent?.subscriptionId, theirWatch.subscriptionID)

        // The other watch's published intent, named as this one's. It is a real automatic capture
        // in the queue, so only its identity distinguishes it from this scope's own work.
        let key = FileMonitoringCoordinator.schedulingKey(for: myWatch)
        try environment.automaticScheduling.signal(key, priority: .background, at: clock.milliseconds)
        clock.advance(3_000)
        let claim = try XCTUnwrap(environment.automaticScheduling.claim(at: clock.milliseconds, among: [key]))
        try environment.automaticScheduling.markPublished(
            key,
            generation: claim.generation,
            intentRef: theirs.intent.itemId,
            at: clock.milliseconds
        )
        try Data("changed".utf8).write(to: myOriginal)

        let reports = await environment.fileMonitoring.reconcile(
            subscriptionIDs: [myWatch.subscriptionID]
        )

        XCTAssertEqual(reports.map(\.subscriptionID), [myWatch.subscriptionID])
        XCTAssertEqual(reports.map(\.state), [.reviewRequired])
        XCTAssertEqual(
            environment.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            1
        )
        XCTAssertEqual(
            environment.fileSubscriptions.all()
                .first { $0.subscriptionID == theirWatch.subscriptionID }?.state,
            .active
        )
    }

    func testPublishedIntentPreparedForAnEarlierDirtyEpochSendsTheSubscriptionToReview() async throws {
        let context = try await activeWatch()
        try Data("changed".utf8).write(to: context.original)
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(3_000)
        _ = await context.environment.fileMonitoring.reconcileAll()
        let readsAfterPublication = context.reader.count
        // The consent binding the intent carries is now one the subscription never issued.
        _ = try context.environment.fileSubscriptions.update(id: context.subscriptionID) {
            $0.dirtyEpoch += 1
        }

        let reports = await context.environment.fileMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.state), [.reviewRequired])
        XCTAssertEqual(context.reader.count, readsAfterPublication)
        XCTAssertEqual(
            context.environment.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            1
        )
        XCTAssertEqual(context.environment.automaticScheduling.state(of: context.key)?.published, true)
    }

    func testLegacyPublicationResolvesItsIntentThroughTheLastQueuedItem() async throws {
        let context = try await activeWatch()
        try Data("changed".utf8).write(to: context.original)
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(3_000)
        _ = await context.environment.fileMonitoring.reconcileAll()
        let published = try XCTUnwrap(
            context.environment.queue.allEntries().first { $0.intent.surface == .automaticFile }
        )
        // A document written before publications carried a ref: the publication stands, but there
        // is nothing in it to reconcile by.
        try dropPublishedIntentRefs(stateRoot: context.stateRoot)

        let clock = TestClock()
        let reader = CountingReader()
        let reopened = try DocumentIntakeEnvironment(
            stateRoot: context.stateRoot,
            bookmarkCodec: BookmarkCodec(),
            reader: reader,
            monotonicClock: clock.reader
        )
        let recovered = try XCTUnwrap(reopened.automaticScheduling.state(of: context.key))
        XCTAssertTrue(recovered.legacyUnresolvedIntent)
        XCTAssertNil(recovered.intentRef)
        XCTAssertEqual(
            reopened.fileSubscriptions.all().first?.lastQueuedItemID,
            published.intent.itemId
        )

        try Data("changed again".utf8).write(to: context.original)
        let subscriptionID = try XCTUnwrap(reopened.fileSubscriptions.all().first?.subscriptionID)
        await reopened.fileMonitoring.markDirty(subscriptionIDs: [subscriptionID])
        clock.advance(3_000)
        let reports = await reopened.fileMonitoring.reconcileAll()

        // The last queued item identified the still-pending intent, so the scope is held rather
        // than sent to review, and no second version is published behind it.
        XCTAssertEqual(reports.map(\.state), [.active])
        XCTAssertEqual(reports.map(\.queued), [false])
        XCTAssertEqual(reader.count, 0)
        XCTAssertEqual(
            reopened.queue.allEntries().filter { $0.intent.surface == .automaticFile }.count,
            1
        )
    }

    func testRestoredCandidateAdoptsTheIntentItsOwnCheckAlreadyCommitted() async throws {
        let candidate = try await restoredCandidateWatch(committingIntent: true)
        let committed = try XCTUnwrap(candidate.intent)

        let reports = await candidate.environment.fileMonitoring.reconcileAll()

        // The unresolved intent is exactly the one this candidate committed, so it is attached
        // rather than read and published again: nothing is read, no identifier is minted, and the
        // queue keeps the single entry it already holds.
        XCTAssertEqual(reports.map(\.state), [.active])
        XCTAssertEqual(reports.map(\.queued), [false])
        XCTAssertEqual(candidate.reader.count, 0)
        XCTAssertEqual(
            candidate.environment.queue.allEntries()
                .filter { $0.intent.surface == .automaticFile }
                .map(\.intent.itemId),
            [committed.intent.itemId]
        )
        // The candidate is now this scope's one unresolved publication, named by that same ref.
        let adopted = try XCTUnwrap(candidate.environment.automaticScheduling.state(of: candidate.key))
        XCTAssertTrue(adopted.published)
        XCTAssertEqual(adopted.intentRef, committed.intent.itemId)
        XCTAssertEqual(adopted.activeGeneration, candidate.generation)
        XCTAssertNil(adopted.dirtyGeneration)
    }

    func testRestoredCandidateAdoptsTheIntentPreparedBeforeEitherWriteSurvivedThenReplaysIt() async throws {
        let candidate = try await restoredCandidateWatch(committingIntent: true)
        let committed = try XCTUnwrap(candidate.intent)
        let epoch = try XCTUnwrap(committed.intent.consent?.dirtyEpoch)
        let subscriptions = candidate.environment.fileSubscriptions
        let subscriptionID = try XCTUnwrap(subscriptions.all().first?.subscriptionID)
        // The cold start where neither of the stopped process's own writes survived: the
        // subscription stands at the epoch the intent was prepared *from*, and nothing it holds
        // names the item that committed for it.
        _ = try subscriptions.update(id: subscriptionID) {
            $0.lastQueuedItemID = nil
            $0.dirtyEpoch = epoch - 1
        }

        let reports = await candidate.environment.fileMonitoring.reconcileAll()

        // The prepared epoch is evidence enough on its own: the intent is attached rather than read
        // and published again, no identifier is minted, and the observation it records is that very
        // item at that very epoch.
        XCTAssertEqual(reports.map(\.state), [.active])
        XCTAssertEqual(reports.map(\.queued), [false])
        XCTAssertEqual(candidate.reader.count, 0)
        XCTAssertEqual(
            candidate.environment.queue.allEntries()
                .filter { $0.intent.surface == .automaticFile }
                .map(\.intent.itemId),
            [committed.intent.itemId]
        )
        XCTAssertEqual(subscriptions.all().map(\.dirtyEpoch), [epoch])
        XCTAssertEqual(subscriptions.all().map(\.lastQueuedItemID), [committed.intent.itemId])
        let adopted = try XCTUnwrap(candidate.environment.automaticScheduling.state(of: candidate.key))
        XCTAssertTrue(adopted.published)
        XCTAssertEqual(adopted.intentRef, committed.intent.itemId)
        XCTAssertEqual(adopted.activeGeneration, candidate.generation)
        XCTAssertNil(adopted.dirtyGeneration)

        // Now stopped between the adoption's own two writes: the observation above is on disk and
        // the publication that should have followed it is not, so the same generation comes back as
        // this scope's candidate with the same intent still unresolved in the queue.
        let scheduling = candidate.environment.automaticScheduling
        _ = try scheduling.cancel(candidate.key, generation: candidate.generation, at: 0)
        let reclaimed = try XCTUnwrap(scheduling.claim(at: 0, among: [candidate.key]))
        XCTAssertEqual(reclaimed.generation, candidate.generation)
        let replayReader = CountingReader()
        let replayed = try DocumentIntakeEnvironment(
            stateRoot: candidate.stateRoot,
            bookmarkCodec: BookmarkCodec(),
            reader: replayReader,
            monotonicClock: TestClock().reader
        )

        let replayReports = await replayed.fileMonitoring.reconcileAll()

        // Applying the adoption twice is applying it once: the recorded observation is itself the
        // replay evidence, so the same intent is attached to the same generation again. Nothing is
        // read, no duplicate is queued, no identifier is minted, and the observation does not move.
        XCTAssertEqual(replayReports.map(\.state), [.active])
        XCTAssertEqual(replayReports.map(\.queued), [false])
        XCTAssertEqual(replayReader.count, 0)
        XCTAssertEqual(
            replayed.queue.allEntries()
                .filter { $0.intent.surface == .automaticFile }
                .map(\.intent.itemId),
            [committed.intent.itemId]
        )
        XCTAssertEqual(replayed.fileSubscriptions.all().map(\.dirtyEpoch), [epoch])
        XCTAssertEqual(replayed.fileSubscriptions.all().map(\.lastQueuedItemID), [committed.intent.itemId])
        let reattached = try XCTUnwrap(replayed.automaticScheduling.state(of: candidate.key))
        XCTAssertTrue(reattached.published)
        XCTAssertEqual(reattached.intentRef, committed.intent.itemId)
        XCTAssertEqual(reattached.activeGeneration, candidate.generation)
        XCTAssertNil(reattached.dirtyGeneration)
    }

    func testAdoptionKeepsTheLaterDirtyGenerationUntilTheAdoptedIntentResolvesIt() async throws {
        let candidate = try await restoredCandidateWatch(committingIntent: true)
        let committed = try XCTUnwrap(candidate.intent)
        let scheduling = candidate.environment.automaticScheduling
        let subscriptionID = try XCTUnwrap(
            candidate.environment.fileSubscriptions.all().first?.subscriptionID
        )
        // An event this process raised behind the recovered candidate: later than the generation the
        // committed intent was prepared for, so it is work of its own and not part of the adoption.
        await candidate.environment.fileMonitoring.markDirty(subscriptionIDs: [subscriptionID])
        let later = candidate.generation + 1
        XCTAssertEqual(scheduling.state(of: candidate.key)?.dirtyGeneration, later)

        let reports = await candidate.environment.fileMonitoring.reconcileAll()

        // The candidate adopts its own committed intent without reading anything, and the signal
        // behind it is neither consumed by that adoption nor published with it.
        XCTAssertEqual(reports.map(\.state), [.active])
        XCTAssertEqual(reports.map(\.queued), [false])
        XCTAssertEqual(candidate.reader.count, 0)
        let adopted = try XCTUnwrap(scheduling.state(of: candidate.key))
        XCTAssertTrue(adopted.published)
        XCTAssertEqual(adopted.intentRef, committed.intent.itemId)
        XCTAssertEqual(adopted.activeGeneration, candidate.generation)
        XCTAssertEqual(adopted.dirtyGeneration, later)

        try admit(
            committed,
            captureID: "dcap-" + String(repeating: "c", count: 64),
            queue: candidate.environment.queue
        )
        _ = await candidate.environment.fileMonitoring.reconcileReceipts()

        // The terminal outcome finishes the adopted generation and drops the publication, which is
        // what exposes that same later generation — still unclaimed, unpublished and now claimable.
        let resolved = try XCTUnwrap(scheduling.state(of: candidate.key))
        XCTAssertNil(resolved.activeGeneration)
        XCTAssertFalse(resolved.published)
        XCTAssertNil(resolved.intentRef)
        XCTAssertEqual(resolved.dirtyGeneration, later)
        XCTAssertEqual(candidate.reader.count, 0)
        let claim = try XCTUnwrap(
            scheduling.claim(
                at: AutomaticSchedulingStateMachine.quietPeriodMilliseconds,
                among: [candidate.key]
            )
        )
        XCTAssertEqual(claim.generation, later)
    }

    func testRestoredCandidateWithNoQueueEntryIsReleasedAndItsWorkIsDueAgain() async throws {
        let candidate = try await restoredCandidateWatch(committingIntent: false)
        let scheduling = candidate.environment.automaticScheduling

        // This entry point claims nothing and reads nothing, so the release is all it did.
        _ = await candidate.environment.fileMonitoring.reconcileReceipts()

        // No intent at all is what proves the candidate never published, so the generation it was
        // claimed for is handed back exactly as a cancellation would hand it back.
        let released = try XCTUnwrap(scheduling.state(of: candidate.key))
        XCTAssertNil(released.activeGeneration)
        XCTAssertFalse(released.published)
        XCTAssertFalse(released.restoredCandidate)
        XCTAssertEqual(released.dirtyGeneration, candidate.generation)
        XCTAssertTrue(released.restoredFromRestart)
        XCTAssertEqual(candidate.reader.count, 0)

        let reports = await candidate.environment.fileMonitoring.reconcileAll()

        // Immediately due: the work the stopped process was reading for is checkable again and is
        // checked without waiting out a quiet period on this process's clock. The bytes are
        // unchanged, so that one read finishes the generation and the scope goes clean.
        XCTAssertEqual(reports.map(\.state), [.active])
        XCTAssertEqual(reports.map(\.queued), [false])
        XCTAssertEqual(candidate.reader.count, 1)
        XCTAssertNil(scheduling.state(of: candidate.key))
        XCTAssertTrue(candidate.environment.queue.allEntries().allSatisfy {
            $0.intent.surface != .automaticFile
        })
    }

    func testRestoredCandidateWhoseIntentReachedAnOutcomeSendsTheSubscriptionToReview() async throws {
        let candidate = try await restoredCandidateWatch(committingIntent: true)
        let committed = try XCTUnwrap(candidate.intent)
        // A terminal entry is evidence too, and an outcome a candidate still claimed to be about to
        // publish is not something this owner may resolve either way.
        try admit(
            committed,
            captureID: "dcap-" + String(repeating: "c", count: 64),
            queue: candidate.environment.queue
        )
        let entries = candidate.environment.queue.allEntries()
        let before = try XCTUnwrap(candidate.environment.automaticScheduling.state(of: candidate.key))

        let reports = await candidate.environment.fileMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.state), [.reviewRequired])
        XCTAssertEqual(candidate.environment.fileSubscriptions.all().map(\.state), [.reviewRequired])
        XCTAssertEqual(candidate.reader.count, 0)
        // Neither the queue nor the candidate is rewritten: both are left exactly as they stand.
        XCTAssertEqual(candidate.environment.queue.allEntries(), entries)
        XCTAssertEqual(candidate.environment.automaticScheduling.state(of: candidate.key), before)
    }

    func testRestoredCandidateWithASecondUnresolvedIntentSendsTheSubscriptionToReview() async throws {
        let candidate = try await restoredCandidateWatch(committingIntent: true)
        let committed = try XCTUnwrap(candidate.intent)
        let queue = candidate.environment.queue
        // A second unresolved intent naming this subscription: the same capture under another
        // identifier, so the candidate committed one of the two and which one is not a guess.
        var second = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: JSONEncoder().encode(committed.intent))
                as? [String: Any]
        )
        second["itemId"] = "ditem-" + String(repeating: "e", count: 64)
        try queue.enqueue(
            intent: try JSONDecoder().decode(
                CaptureIntentV3.self,
                from: try JSONSerialization.data(withJSONObject: second)
            ),
            payload: try Data(contentsOf: queue.payloadURL(for: committed.intent.itemId))
        )
        let entries = queue.allEntries()
        XCTAssertEqual(entries.filter { $0.intent.surface == .automaticFile }.count, 2)
        let before = try XCTUnwrap(candidate.environment.automaticScheduling.state(of: candidate.key))

        let reports = await candidate.environment.fileMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.state), [.reviewRequired])
        XCTAssertEqual(candidate.environment.fileSubscriptions.all().map(\.state), [.reviewRequired])
        XCTAssertEqual(candidate.reader.count, 0)
        // Neither the queue nor the candidate is rewritten: both are left exactly as they stand.
        XCTAssertEqual(queue.allEntries(), entries)
        XCTAssertEqual(candidate.environment.automaticScheduling.state(of: candidate.key), before)
    }

    func testRestoredCandidateIntentPreparedForAnotherDirtyEpochSendsTheSubscriptionToReview() async throws {
        let candidate = try await restoredCandidateWatch(committingIntent: true)
        let subscriptionID = try XCTUnwrap(
            candidate.environment.fileSubscriptions.all().first?.subscriptionID
        )
        // The one unresolved intent is this scope's own automatic capture, but prepared for an
        // epoch the subscription has since moved past: neither the replay nor the next epoch a
        // single check could have produced, so it did not come from this candidate.
        _ = try candidate.environment.fileSubscriptions.update(id: subscriptionID) {
            $0.dirtyEpoch += 1
        }
        let entries = candidate.environment.queue.allEntries()
        let before = try XCTUnwrap(candidate.environment.automaticScheduling.state(of: candidate.key))

        let reports = await candidate.environment.fileMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.state), [.reviewRequired])
        XCTAssertEqual(candidate.environment.fileSubscriptions.all().map(\.state), [.reviewRequired])
        XCTAssertEqual(candidate.reader.count, 0)
        XCTAssertEqual(candidate.environment.queue.allEntries(), entries)
        XCTAssertEqual(candidate.environment.automaticScheduling.state(of: candidate.key), before)
    }

    // MARK: - Support

    private struct RestoredCandidate {
        /// Reopened from the same state root, so the candidate, the queue and the subscription all
        /// come off disk.
        let environment: DocumentIntakeEnvironment
        let reader: CountingReader
        let key: AutomaticSchedulingKey
        let generation: UInt64
        /// The automatic intent the stopped process committed, when it got that far.
        let intent: CaptureQueueEntry?
        /// The same state root, so a test can stop this process too and reopen it again.
        let stateRoot: URL
    }

    /// One active watch whose scope a restart recovers as an active, unpublished candidate: the
    /// generation a stopped process claimed and whose publication never reached the record.
    ///
    /// `committingIntent` lets that process's check commit its automatic intent to the queue first,
    /// so the candidate comes back with an unresolved intent of its own and nothing naming it.
    private func restoredCandidateWatch(committingIntent: Bool) async throws -> RestoredCandidate {
        let context = try await activeWatch()
        let scheduling = context.environment.automaticScheduling
        var intent: CaptureQueueEntry?
        if committingIntent {
            intent = try await publishOneAutomaticChange(context)
            // The queue commit and this owner's observation survived the stop and the scheduling
            // publication did not, so that publication comes back off the record and its generation
            // back to the claim it was taken under.
            let published = try XCTUnwrap(scheduling.state(of: context.key)?.activeGeneration)
            _ = try scheduling.cancel(
                context.key, generation: published, at: context.clock.milliseconds
            )
        } else {
            await context.environment.fileMonitoring.markDirty(
                subscriptionIDs: [context.subscriptionID]
            )
            context.clock.advance(3_000)
        }
        // The claim the stopped process was carrying, which is the durable shape a restart recovers
        // as this scope's candidate.
        let claim = try XCTUnwrap(
            scheduling.claim(at: context.clock.milliseconds, among: [context.key])
        )
        let reader = CountingReader()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: context.stateRoot,
            bookmarkCodec: BookmarkCodec(),
            reader: reader,
            monotonicClock: TestClock().reader,
            currentCapture: StandingCapture()
        )
        let recovered = try XCTUnwrap(environment.automaticScheduling.state(of: context.key))
        XCTAssertTrue(recovered.restoredCandidate)
        XCTAssertFalse(recovered.published)
        XCTAssertEqual(recovered.activeGeneration, claim.generation)
        return RestoredCandidate(
            environment: environment,
            reader: reader,
            key: context.key,
            generation: claim.generation,
            intent: intent,
            stateRoot: context.stateRoot
        )
    }

    private struct WatchContext {
        let environment: DocumentIntakeEnvironment
        let clock: TestClock
        let reader: CountingReader
        let original: URL
        let stateRoot: URL
        let subscriptionID: String
        let key: AutomaticSchedulingKey
        /// Reads the baseline submission already spent, so a test can say "nothing was read".
        let baselineReads: Int
    }

    /// Rewrites the durable scheduling document as a build that never wrote publication refs would
    /// have left it, so reopening it recovers a legacy unresolved intent.
    private func dropPublishedIntentRefs(stateRoot: URL) throws {
        let file = stateRoot
            .appendingPathComponent("automatic-scheduling", isDirectory: true)
            .appendingPathComponent("automatic-scheduling-v1.json")
        var document = try XCTUnwrap(
            JSONSerialization.jsonObject(with: try Data(contentsOf: file)) as? [String: Any]
        )
        let entries = try XCTUnwrap(document["entries"] as? [[String: Any]])
        document["entries"] = entries.map { entry in
            var entry = entry
            entry.removeValue(forKey: "intentRef")
            return entry
        }
        try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
            .write(to: file, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
    }

    /// One admitted baseline watch on an unchanged original, with nothing signalled yet.
    private func activeWatch() async throws -> WatchContext {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("watched.txt")
        try Data("baseline".utf8).write(to: original)
        let clock = TestClock()
        let reader = CountingReader()
        let stateRoot = root.appendingPathComponent("state", isDirectory: true)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: stateRoot,
            bookmarkCodec: BookmarkCodec(),
            reader: reader,
            monotonicClock: clock.reader,
            currentCapture: StandingCapture()
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
            stateRoot: stateRoot,
            subscriptionID: value.subscriptionID,
            key: FileMonitoringCoordinator.schedulingKey(for: value),
            baselineReads: reader.count
        )
    }

    /// Signals this watch and lets the quiet period elapse, so exactly one claimed check runs.
    private func checkOnce(_ context: WatchContext) async throws -> [FileMonitoringReport] {
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(3_000)
        return await context.environment.fileMonitoring.reconcileAll()
    }

    /// Nothing a finished check leaves behind: no checked time, no completion, and the capture
    /// this scope already holds untouched. The generation is owed again as dirty work on the
    /// signal times it already had, which is what the success path instead resolves and drops.
    private func assertNothingWasRecorded(
        _ context: WatchContext,
        before: FileSubscription,
        file: StaticString = #filePath,
        line: UInt = #line
    ) throws {
        let after = try XCTUnwrap(
            context.environment.fileSubscriptions.all().first, file: file, line: line
        )
        XCTAssertNil(after.lastCheckedAt, file: file, line: line)
        XCTAssertTrue(after.needsReconciliation, file: file, line: line)
        XCTAssertEqual(
            after.currentSuccessfulCaptureID, before.currentSuccessfulCaptureID, file: file, line: line
        )
        XCTAssertEqual(after.lastObservedDigest, before.lastObservedDigest, file: file, line: line)
        XCTAssertEqual(
            after.lastObservedByteLength, before.lastObservedByteLength, file: file, line: line
        )
        XCTAssertEqual(after.lastObservedAt, before.lastObservedAt, file: file, line: line)
        XCTAssertEqual(after.dirtyEpoch, before.dirtyEpoch, file: file, line: line)
        XCTAssertEqual(after.lastQueuedItemID, before.lastQueuedItemID, file: file, line: line)
        XCTAssertTrue(
            context.environment.queue.allEntries().allSatisfy { $0.intent.surface != .automaticFile },
            file: file,
            line: line
        )
        let scheduled = try XCTUnwrap(
            context.environment.automaticScheduling.state(of: context.key), file: file, line: line
        )
        XCTAssertEqual(scheduled.dirtyGeneration, 1, file: file, line: line)
        XCTAssertNil(scheduled.activeGeneration, file: file, line: line)
        XCTAssertFalse(scheduled.published, file: file, line: line)
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

    /// Changes the watched original and lets one check publish it, returning that intent.
    private func publishOneAutomaticChange(_ context: WatchContext) async throws -> CaptureQueueEntry {
        try Data("changed".utf8).write(to: context.original)
        await context.environment.fileMonitoring.markDirty(subscriptionIDs: [context.subscriptionID])
        context.clock.advance(3_000)
        _ = await context.environment.fileMonitoring.reconcileAll()
        return try XCTUnwrap(
            context.environment.queue.allEntries().first { $0.intent.surface == .automaticFile }
        )
    }

    private func setPermissions(_ permissions: Int, of directory: URL) throws {
        try FileManager.default.setAttributes(
            [.posixPermissions: permissions], ofItemAtPath: directory.path
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
