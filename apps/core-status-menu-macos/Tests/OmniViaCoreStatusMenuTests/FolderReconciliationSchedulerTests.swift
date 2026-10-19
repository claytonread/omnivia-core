@testable import DocumentIntakeMacOS
import Foundation
import XCTest

/// WP-11 slice 3: the durable periodic schedule and the coverage truth behind it. Every test drives
/// a real `FolderSubscriptionStore`, a real `FolderObservationLedger` and a real state directory,
/// and rebuilds the scheduler from disk where restart matters.
final class FolderReconciliationSchedulerTests: XCTestCase {
    /// A clock the test moves by hand. The scheduler never reads the wall clock.
    private final class Clock: @unchecked Sendable {
        private let lock = NSLock()
        private var value: Date

        init(_ value: Date) { self.value = value }

        var now: Date { lock.withLock { value } }

        func advance(_ seconds: TimeInterval) { lock.withLock { value += seconds } }
    }

    private struct Lane {
        let directory: URL
        let store: FolderSubscriptionStore
        let ledger: FolderObservationLedger
        let gate: PublicationGate
        let clock: Clock
    }

    private static let uuidA = "11111111-1111-4111-8111-111111111111"
    private static let device: Int64 = 16_777_230
    private static let epoch = Date(timeIntervalSince1970: 1_760_000_000)

    // MARK: - Lane

    private func lane(_ ids: [String] = ["sub-a", "sub-b"]) throws -> Lane {
        let directory = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("omnivia-reconciliation-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock {
            for name in ["schedule", "observation", "subscriptions"] {
                try? FileManager.default.setAttributes(
                    [.posixPermissions: 0o700],
                    ofItemAtPath: directory.appendingPathComponent(name).path
                )
            }
            try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
            try? FileManager.default.removeItem(at: directory)
        }
        let gate = PublicationGate()
        let store = try FolderSubscriptionStore(
            stateDirectory: directory.appendingPathComponent("subscriptions"),
            gate: gate
        )
        for id in ids { try store.register(Self.subscription(id)) }
        let ledger = try FolderObservationLedger(
            stateDirectory: directory.appendingPathComponent("observation"),
            subscriptions: store,
            gate: gate
        )
        // A stream whose opening full reconciliations are already acknowledged, so a later
        // obligation is unambiguously the scheduler's.
        let status = try ledger.beginStream(
            identity: FolderObservationStreamIdentity(deviceID: Self.device, streamUUID: Self.uuidA),
            startEventID: 100,
            subscriptions: ids.map {
                FolderObservationApplicableSubscription(subscriptionID: $0, configGeneration: 1)
            }
        )
        for id in ids {
            let revision = try XCTUnwrap(ledger.obligation(subscriptionID: id)).revision
            try ledger.acknowledge(
                subscriptionID: id,
                configGeneration: 1,
                observationEpoch: status.observationEpoch,
                completion: .full(throughRevision: revision)
            )
        }
        return Lane(directory: directory, store: store, ledger: ledger, gate: gate, clock: Clock(Self.epoch))
    }

    private func scheduler(
        _ lane: Lane,
        jitter: @escaping @Sendable (String, FolderReconciliationKind) -> Double = { _, _ in 0 },
        observation: FolderObservationLedger? = nil
    ) throws -> FolderReconciliationScheduler {
        let clock = lane.clock
        return try FolderReconciliationScheduler(
            stateDirectory: lane.directory.appendingPathComponent("schedule"),
            subscriptions: lane.store,
            observation: observation ?? lane.ledger,
            clock: { clock.now },
            jitter: jitter,
            gate: lane.gate
        )
    }

    private static func subscription(_ id: String) -> FolderSubscription {
        FolderSubscription(
            subscriptionID: id,
            rootRef: "root-\(id)",
            rootDisplayName: "Root",
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
            configGeneration: 1,
            consentGeneration: 1,
            consentRecordedAt: "2026-10-14T00:00:00Z",
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

    private func setState(_ lane: Lane, _ id: String, _ state: FolderSubscriptionState) throws {
        try lane.store.update(id: id) { $0.state = state }
    }

    /// Clears everything the scheduler armed for one subscription, with complete coverage.
    private func settle(
        _ scheduler: FolderReconciliationScheduler,
        _ id: String,
        examined: Int = 3
    ) throws {
        for kind in FolderReconciliationKind.allCases {
            try scheduler.complete(
                subscriptionID: id,
                kind: kind,
                result: FolderReconciliationResult(examined: examined)
            )
        }
    }

    private func demand(
        _ id: String,
        _ kind: FolderReconciliationKind,
        _ trigger: FolderReconciliationTrigger
    ) -> FolderReconciliationScheduler.Demand {
        FolderReconciliationScheduler.Demand(subscriptionID: id, kind: kind, trigger: trigger)
    }

    // MARK: - Interval boundaries

    func testTheDesignIntervalsAreFifteenMinutesAndTwentyFourHours() {
        XCTAssertEqual(FolderReconciliationKind.metadata.interval, 15 * 60)
        XCTAssertEqual(FolderReconciliationKind.content.interval, 24 * 60 * 60)
    }

    func testAKindBecomesDueExactlyAtItsIntervalAndNotAnInstantBefore() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        XCTAssertEqual(scheduler.due(), [])

        lane.clock.advance(15 * 60 - 0.001)
        XCTAssertEqual(scheduler.due(), [])

        lane.clock.advance(0.001)
        XCTAssertEqual(scheduler.due(), [demand("sub-a", .metadata, .due)])

        // Content is still inside its own, much longer window.
        lane.clock.advance(24 * 60 * 60 - 15 * 60 - 0.001)
        XCTAssertEqual(scheduler.due(), [demand("sub-a", .metadata, .due)])
        lane.clock.advance(0.001)
        XCTAssertEqual(
            scheduler.due(),
            [demand("sub-a", .metadata, .due), demand("sub-a", .content, .due)]
        )
    }

    func testASubscriptionThatHasNeverRunIsDueImmediately() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        XCTAssertEqual(
            scheduler.due(),
            [demand("sub-a", .metadata, .due), demand("sub-a", .content, .due)]
        )
        let state = scheduler.coverage(subscriptionID: "sub-a", kind: .metadata)
        XCTAssertNil(state.dueAt)
        XCTAssertNil(state.lastCompleteAt)
        XCTAssertTrue(state.isOverdue(at: lane.clock.now))
        XCTAssertFalse(state.isUpToDate(at: lane.clock.now))
    }

    // MARK: - Jitter

    func testJitterStretchesTheIntervalByAtMostTwentyPercent() throws {
        XCTAssertEqual(FolderReconciliationScheduler.maximumJitterFraction, 0.2)
        for (fraction, metadata) in [(0.0, 900.0), (0.5, 990.0), (1.0, 1080.0)] {
            let lane = try lane(["sub-a"])
            let scheduler = try scheduler(lane, jitter: { _, _ in fraction })
            try settle(scheduler, "sub-a")
            XCTAssertEqual(
                scheduler.coverage(subscriptionID: "sub-a", kind: .metadata).dueAt,
                Self.epoch.addingTimeInterval(metadata)
            )
            XCTAssertEqual(
                scheduler.coverage(subscriptionID: "sub-a", kind: .content).dueAt,
                Self.epoch.addingTimeInterval(86_400 * (1 + 0.2 * fraction))
            )
        }
    }

    func testAnInjectedFractionOutsideTheBoundIsClamped() {
        for fraction in [5.0, 1.0] {
            XCTAssertEqual(
                FolderReconciliationScheduler.nextDue(.metadata, from: Self.epoch, fraction: fraction),
                Self.epoch.addingTimeInterval(1080)
            )
        }
        for fraction in [-1.0, 0.0, Double.nan] {
            XCTAssertEqual(
                FolderReconciliationScheduler.nextDue(.metadata, from: Self.epoch, fraction: fraction),
                Self.epoch.addingTimeInterval(900)
            )
        }
    }

    func testTheDefaultJitterIsDeterministicInBoundsAndSpreadAcrossSubscriptions() {
        let ids = (0..<64).map { "sub-\($0)" }
        var fractions: Set<Double> = []
        for id in ids {
            for kind in FolderReconciliationKind.allCases {
                let value = FolderReconciliationScheduler.deterministicJitter(id, kind)
                XCTAssertEqual(value, FolderReconciliationScheduler.deterministicJitter(id, kind))
                XCTAssertTrue(value >= 0 && value < 1, "\(id) \(kind) produced \(value)")
                fractions.insert(value)
            }
        }
        // Subscriptions established together must not all come due in the same instant.
        XCTAssertGreaterThan(fractions.count, 32)
    }

    func testTheSameSubscriptionKeepsItsSlotAcrossRestart() throws {
        let lane = try lane(["sub-a"])
        let first = try scheduler(lane, jitter: FolderReconciliationScheduler.deterministicJitter)
        try settle(first, "sub-a")
        let due = first.coverage(subscriptionID: "sub-a", kind: .metadata).dueAt
        let second = try scheduler(lane, jitter: FolderReconciliationScheduler.deterministicJitter)
        try second.complete(
            subscriptionID: "sub-a",
            kind: .metadata,
            result: FolderReconciliationResult(examined: 3)
        )
        XCTAssertEqual(second.coverage(subscriptionID: "sub-a", kind: .metadata).dueAt, due)
    }

    // MARK: - Immediate triggers

    func testEveryImmediateTriggerSchedulesBothKindsAtOnce() throws {
        let immediate = FolderReconciliationTrigger.allCases.filter(\.isImmediate)
        XCTAssertEqual(
            Set(immediate),
            [.startup, .wake, .remount, .resume, .sourceAccessRecovered, .observationLost]
        )
        for trigger in immediate {
            let lane = try lane(["sub-a"])
            let scheduler = try scheduler(lane)
            try settle(scheduler, "sub-a")
            XCTAssertEqual(scheduler.due(), [])

            let armed = try scheduler.request(subscriptionIDs: ["sub-a"], trigger: trigger)
            XCTAssertEqual(
                armed,
                [demand("sub-a", .metadata, trigger), demand("sub-a", .content, trigger)]
            )
            // Due now, whatever the interval says, and durably so.
            XCTAssertEqual(scheduler.due(), armed)
            XCTAssertEqual(
                scheduler.coverage(subscriptionID: "sub-a", kind: .metadata).pendingTrigger,
                trigger
            )
            XCTAssertEqual(
                ledgerReasons(lane, "sub-a"),
                [.immediate],
                "\(trigger) must merge the scheduler's immediate obligation"
            )
        }
    }

    func testDueIsNotATriggerACallerMayRequest() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        XCTAssertThrowsError(try scheduler.request(subscriptionIDs: ["sub-a"], trigger: .due)) {
            XCTAssertEqual($0 as? FolderReconciliationError, .invalidTrigger)
        }
    }

    func testAnImmediateTriggerLeavesWorkThatIsAlreadyArmedAlone() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try scheduler.schedule()
        let revision = try XCTUnwrap(lane.ledger.obligation(subscriptionID: "sub-a")).revision

        XCTAssertEqual(try scheduler.request(subscriptionIDs: ["sub-a"], trigger: .wake), [])
        XCTAssertEqual(
            scheduler.coverage(subscriptionID: "sub-a", kind: .metadata).pendingTrigger,
            .due
        )
        // The outstanding full reconciliation already covers the wake.
        XCTAssertEqual(lane.ledger.obligation(subscriptionID: "sub-a")?.revision, revision)
    }

    func testOnlyTheNamedSubscriptionsAreScheduled() throws {
        let lane = try lane()
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        try settle(scheduler, "sub-b")
        try scheduler.request(subscriptionIDs: ["sub-a"], trigger: .remount)
        XCTAssertEqual(scheduler.coverage(subscriptionID: "sub-a", kind: .metadata).pendingTrigger, .remount)
        XCTAssertNil(scheduler.coverage(subscriptionID: "sub-b", kind: .metadata).pendingTrigger)
    }

    // MARK: - The event-bridge seam

    func testALostObservationFromTheBridgeSchedulesImmediateFullWork() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        let armed = try scheduler.observe(
            FolderEventSignal(subscriptionIDs: ["sub-a"], requiresFullReconciliation: true)
        )
        XCTAssertEqual(armed.map(\.trigger), [.observationLost, .observationLost])
        XCTAssertEqual(ledgerReasons(lane, "sub-a"), [.immediate])
    }

    func testAnOrdinaryEventSignalSchedulesNothing() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        // The bridge has already persisted the scope obligations this signal came from; the
        // scheduler must not add a full reconciliation on top of them.
        XCTAssertEqual(
            try scheduler.observe(
                FolderEventSignal(subscriptionIDs: ["sub-a"], requiresFullReconciliation: false)
            ),
            []
        )
        XCTAssertNil(lane.ledger.obligation(subscriptionID: "sub-a"))
        XCTAssertEqual(scheduler.due(), [])
    }

    // MARK: - Pause and resume

    func testAPausedSubscriptionSchedulesNoAutomaticMetadataOrContentRead() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        try setState(lane, "sub-a", .paused)
        lane.clock.advance(48 * 60 * 60)

        XCTAssertEqual(scheduler.due(), [])
        XCTAssertEqual(try scheduler.schedule(), [])
        for trigger in FolderReconciliationTrigger.allCases.filter(\.isImmediate) {
            XCTAssertEqual(try scheduler.request(subscriptionIDs: ["sub-a"], trigger: trigger), [])
        }
        XCTAssertNil(lane.ledger.obligation(subscriptionID: "sub-a"))
        XCTAssertNil(scheduler.coverage(subscriptionID: "sub-a", kind: .metadata).pendingTrigger)
    }

    func testEveryNonActiveStateIsLeftUnscheduled() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        for state in [
            FolderSubscriptionState.paused, .stopped, .reviewRequired, .needsAccess, .waitingForCore,
        ] {
            try setState(lane, "sub-a", state)
            XCTAssertEqual(try scheduler.schedule(), [], "\(state) must not be scheduled")
        }
        XCTAssertNil(lane.ledger.obligation(subscriptionID: "sub-a"))
    }

    func testResumeMakesWorkDueImmediatelyAgain() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        try setState(lane, "sub-a", .paused)
        XCTAssertEqual(try scheduler.schedule(), [])

        try setState(lane, "sub-a", .active)
        let armed = try scheduler.request(subscriptionIDs: ["sub-a"], trigger: .resume)
        XCTAssertEqual(armed.map(\.trigger), [.resume, .resume])
        XCTAssertEqual(ledgerReasons(lane, "sub-a"), [.immediate])
    }

    // MARK: - Restart

    func testArmedWorkSurvivesRestart() throws {
        let lane = try lane(["sub-a"])
        let first = try scheduler(lane)
        try settle(first, "sub-a")
        try first.request(subscriptionIDs: ["sub-a"], trigger: .wake)

        let second = try scheduler(lane)
        XCTAssertEqual(
            second.due(),
            [demand("sub-a", .metadata, .wake), demand("sub-a", .content, .wake)]
        )
    }

    func testCompletedCoverageSurvivesRestart() throws {
        let lane = try lane(["sub-a"])
        let first = try scheduler(lane)
        try first.complete(
            subscriptionID: "sub-a",
            kind: .metadata,
            result: FolderReconciliationResult(examined: 7, notResident: 2)
        )
        let state = try scheduler(lane).coverage(subscriptionID: "sub-a", kind: .metadata)
        XCTAssertEqual(state.lastOutcome, .partial)
        XCTAssertEqual(state.examined, 7)
        XCTAssertEqual(state.notResident, 2)
        XCTAssertNil(state.lastCompleteAt)
        XCTAssertEqual(state.lastAttemptAt, Self.epoch)
        XCTAssertEqual(state.dueAt, Self.epoch.addingTimeInterval(900))
    }

    func testARestartDoesNotReArmWorkThatWasAlreadyFinished() throws {
        let lane = try lane(["sub-a"])
        let first = try scheduler(lane)
        try settle(first, "sub-a")
        let revision = lane.ledger.obligation(subscriptionID: "sub-a")?.revision

        let second = try scheduler(lane)
        XCTAssertEqual(try second.schedule(), [])
        XCTAssertEqual(lane.ledger.obligation(subscriptionID: "sub-a")?.revision, revision)
    }

    func testANewerBuildsDocumentIsRefused() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        let file = lane.directory
            .appendingPathComponent("schedule")
            .appendingPathComponent("folder-reconciliation-v1.json")
        var document = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any]
        )
        document["schema"] = "omnivia.folder-reconciliation.v2"
        try JSONSerialization.data(withJSONObject: document).write(to: file)
        XCTAssertThrowsError(try self.scheduler(lane)) {
            XCTAssertEqual($0 as? FolderReconciliationError, .unsupportedSchema)
        }
    }

    // MARK: - Coverage truth

    func testADueIntervalIsNotAPromiseOfCompletedCoverage() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try scheduler.schedule()
        let armed = scheduler.coverage(subscriptionID: "sub-a", kind: .metadata)
        // Scheduled, not covered: nothing may read this as up to date.
        XCTAssertEqual(armed.pendingTrigger, .due)
        XCTAssertNil(armed.lastCompleteAt)
        XCTAssertTrue(armed.isDue(at: lane.clock.now))
        XCTAssertTrue(armed.isOverdue(at: lane.clock.now))
        XCTAssertFalse(armed.isUpToDate(at: lane.clock.now))
    }

    func testCompleteCoverageIsUpToDateUntilItsIntervalElapses() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        let state = scheduler.coverage(subscriptionID: "sub-a", kind: .metadata)
        XCTAssertEqual(state.lastOutcome, .complete)
        XCTAssertEqual(state.lastCompleteAt, Self.epoch)
        XCTAssertEqual(state.uncovered, 0)
        XCTAssertTrue(state.isUpToDate(at: lane.clock.now))
        XCTAssertFalse(state.isOverdue(at: lane.clock.now))
        // Due again is not overdue: the last pass really did cover the whole scope.
        XCTAssertFalse(state.isUpToDate(at: Self.epoch.addingTimeInterval(900)))
        XCTAssertTrue(state.isDue(at: Self.epoch.addingTimeInterval(900)))
        XCTAssertFalse(state.isOverdue(at: Self.epoch.addingTimeInterval(900)))
    }

    /// Incomplete coverage is attention from the instant it is reported, not when its next interval
    /// happens to come round. A folder whose members could not all be covered is not quietly fine
    /// for the rest of the interval because the clock has not moved.
    func testIncompleteCoverageIsOverdueAsSoonAsItIsReported() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try scheduler.complete(
            subscriptionID: "sub-a",
            kind: .metadata,
            result: FolderReconciliationResult(examined: 4, unreadable: 1)
        )
        let state = scheduler.coverage(subscriptionID: "sub-a", kind: .metadata)
        XCTAssertEqual(state.lastOutcome, .partial)
        XCTAssertFalse(state.isUpToDate(at: lane.clock.now))
        XCTAssertTrue(state.isOverdue(at: lane.clock.now))
        XCTAssertTrue(state.isOverdue(at: Self.epoch.addingTimeInterval(900)))
        // Partial coverage still spends its kind's interval: re-reading every resident member to
        // learn that a provider-only one is still provider-only would cost the pass for nothing.
        XCTAssertFalse(state.isDue(at: lane.clock.now))
        XCTAssertEqual(state.dueAt, Self.epoch.addingTimeInterval(900))
    }

    func testCompletingClearsTheArmedTriggerAndReschedules() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try scheduler.request(subscriptionIDs: ["sub-a"], trigger: .startup)
        try scheduler.complete(
            subscriptionID: "sub-a",
            kind: .metadata,
            result: FolderReconciliationResult(examined: 2)
        )
        XCTAssertEqual(scheduler.due(), [demand("sub-a", .content, .startup)])
        XCTAssertNil(scheduler.coverage(subscriptionID: "sub-a", kind: .metadata).pendingTrigger)
    }

    func testCompletingAnUnknownSubscriptionIsRefused() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        XCTAssertThrowsError(
            try scheduler.complete(
                subscriptionID: "sub-missing",
                kind: .metadata,
                result: FolderReconciliationResult(examined: 0)
            )
        ) { XCTAssertEqual($0 as? FolderReconciliationError, .unknownSubscription) }
    }

    func testResultCountsBeyondTheDesignBoundAreRefused() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        let limit = FolderScopeLimits.standard.maximumEligibleFiles
        for result in [
            FolderReconciliationResult(examined: -1),
            FolderReconciliationResult(examined: Int.max),
            FolderReconciliationResult(examined: 1, notResident: Int.max),
            FolderReconciliationResult(examined: 1, unreadable: Int.max),
            FolderReconciliationResult(examined: 1, notResident: Int.max / 2, unreadable: Int.max / 2),
            FolderReconciliationResult(examined: limit + 1),
            // The categories partition one eligible population, so the bound is on their total.
            FolderReconciliationResult(examined: limit, notResident: 1),
            FolderReconciliationResult(examined: limit, unreadable: 1),
            FolderReconciliationResult(examined: limit, notResident: limit, unreadable: limit),
        ] {
            XCTAssertThrowsError(
                try scheduler.complete(subscriptionID: "sub-a", kind: .metadata, result: result)
            ) { XCTAssertEqual($0 as? FolderReconciliationError, .invalidState) }
        }
        XCTAssertNil(scheduler.coverage(subscriptionID: "sub-a", kind: .metadata).lastOutcome)

        // A total of exactly the bound is reportable, and `uncovered` sums it without overflowing.
        let state = try scheduler.complete(
            subscriptionID: "sub-a",
            kind: .metadata,
            result: FolderReconciliationResult(
                examined: limit - 2, notResident: 1, unreadable: 1
            )
        )
        XCTAssertEqual(state.examined, limit - 2)
        XCTAssertEqual(state.uncovered, 2)

        // A tampered document is judged by the same total, so an entry whose fields each look
        // sane but whose sum does not is refused before it is read back.
        for notResident in [Int.max, 2] {
            try tamper(lane, ["notResident": notResident])
            XCTAssertThrowsError(try self.scheduler(lane), "notResident \(notResident)") {
                XCTAssertEqual($0 as? FolderReconciliationError, .invalidState)
            }
        }
    }

    // MARK: - Provider availability

    func testProviderAndResidencyGapsAreNeverReportedAsCompleteCoverage() throws {
        let cases: [(FolderReconciliationResult, FolderReconciliationOutcome, Int)] = [
            (FolderReconciliationResult(examined: 5), .complete, 0),
            // Cloud-only bytes are not downloaded to close the gap, so coverage stays partial.
            (FolderReconciliationResult(examined: 5, notResident: 3), .partial, 3),
            (FolderReconciliationResult(examined: 5, unreadable: 2), .partial, 2),
            (FolderReconciliationResult(examined: 0, providerAvailable: false), .providerUnavailable, 0),
            (FolderReconciliationResult(examined: 2, failed: true), .failed, 0),
        ]
        for (result, outcome, uncovered) in cases {
            XCTAssertEqual(result.outcome, outcome)
            let lane = try lane(["sub-a"])
            let scheduler = try scheduler(lane)
            let state = try scheduler.complete(
                subscriptionID: "sub-a",
                kind: .content,
                result: result
            )
            XCTAssertEqual(state.lastOutcome, outcome)
            XCTAssertEqual(state.uncovered, uncovered)
            XCTAssertEqual(state.isUpToDate(at: lane.clock.now), outcome == .complete)
            XCTAssertEqual(state.lastCompleteAt, outcome == .complete ? Self.epoch : nil)
        }
    }

    func testAnUnavailableProviderMustReportThatNothingWasCovered() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        // Nothing could be covered, so a count claiming otherwise is a contradiction, not a gap.
        for result in [
            FolderReconciliationResult(examined: 1, providerAvailable: false),
            FolderReconciliationResult(examined: 0, notResident: 4, providerAvailable: false),
            FolderReconciliationResult(examined: 0, unreadable: 2, providerAvailable: false),
            FolderReconciliationResult(examined: 1, notResident: 4, providerAvailable: false),
            FolderReconciliationResult(examined: 3, providerAvailable: false, failed: true),
        ] {
            XCTAssertThrowsError(
                try scheduler.complete(subscriptionID: "sub-a", kind: .content, result: result)
            ) { XCTAssertEqual($0 as? FolderReconciliationError, .invalidState) }
        }
        XCTAssertNil(scheduler.coverage(subscriptionID: "sub-a", kind: .content).lastOutcome)

        let state = try scheduler.complete(
            subscriptionID: "sub-a",
            kind: .content,
            result: FolderReconciliationResult(examined: 0, providerAvailable: false)
        )
        XCTAssertEqual(state.lastOutcome, .providerUnavailable)
        XCTAssertEqual(state.uncovered, 0)
    }

    func testATamperedProviderUnavailableEntryIsRefused() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try scheduler.complete(
            subscriptionID: "sub-a",
            kind: .content,
            result: FolderReconciliationResult(examined: 0, providerAvailable: false)
        )
        // Provider availability is persisted as the outcome, so the stored counts are judged
        // against it on the way back in.
        for field in ["examined", "notResident", "unreadable"] {
            try tamper(lane, [field: 5])
            XCTAssertThrowsError(try self.scheduler(lane), field) {
                XCTAssertEqual($0 as? FolderReconciliationError, .invalidState)
            }
            try tamper(lane, [field: 0])
        }
        // And a partial entry's own counts are still readable: the rule is outcome-specific.
        try tamper(lane, ["lastOutcome": "partial", "examined": 4, "notResident": 1])
        let state = try self.scheduler(lane).coverage(subscriptionID: "sub-a", kind: .content)
        XCTAssertEqual(state.lastOutcome, .partial)
        XCTAssertEqual(state.uncovered, 1)
    }

    func testAnUnavailableProviderDoesNotStopTheNextPassComingDue() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try scheduler.complete(
            subscriptionID: "sub-a",
            kind: .content,
            result: FolderReconciliationResult(examined: 3)
        )
        try scheduler.complete(
            subscriptionID: "sub-a",
            kind: .metadata,
            result: FolderReconciliationResult(examined: 0, providerAvailable: false)
        )
        XCTAssertEqual(scheduler.due(), [])
        lane.clock.advance(15 * 60)
        XCTAssertEqual(try scheduler.schedule(), [demand("sub-a", .metadata, .due)])
    }

    // MARK: - The observation ledger

    func testSchedulingMergesAFullObligationWithoutAdvancingTheEventCursor() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try lane.ledger.ingest(
            FolderObservationBatch(
                identity: FolderObservationStreamIdentity(deviceID: Self.device, streamUUID: Self.uuidA),
                observationEpoch: 1,
                events: [
                    FolderObservationEvent(
                        eventID: 140,
                        subscriptionID: "sub-a",
                        configGeneration: 1,
                        reason: .scope(FolderObservationScope(kind: .member, components: ["a.txt"]))
                    ),
                ]
            )
        )
        XCTAssertEqual(lane.ledger.stream(deviceID: Self.device)?.lastCoveredEventID, 140)

        try scheduler.schedule()
        let obligation = try XCTUnwrap(lane.ledger.obligation(subscriptionID: "sub-a"))
        XCTAssertNotNil(obligation.fullRevision)
        XCTAssertEqual(obligation.reasons, [.scheduled])
        // A schedule says nothing about which events were covered.
        XCTAssertEqual(lane.ledger.stream(deviceID: Self.device)?.lastCoveredEventID, 140)
    }

    func testAScheduledObligationOutlivesAnAcknowledgementOfOlderWork() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        let identity = FolderObservationStreamIdentity(deviceID: Self.device, streamUUID: Self.uuidA)
        try lane.ledger.ingest(
            FolderObservationBatch(
                identity: identity,
                observationEpoch: 1,
                events: [
                    FolderObservationEvent(
                        eventID: 140,
                        subscriptionID: "sub-a",
                        configGeneration: 1,
                        reason: .scope(FolderObservationScope(kind: .member, components: ["a.txt"]))
                    ),
                ]
            )
        )
        let eventRevision = try XCTUnwrap(lane.ledger.obligation(subscriptionID: "sub-a")).revision
        try scheduler.request(subscriptionIDs: ["sub-a"], trigger: .wake)
        let scheduled = try XCTUnwrap(lane.ledger.obligation(subscriptionID: "sub-a")).revision
        XCTAssertGreaterThan(scheduled, eventRevision)

        // A reconciliation that only finished the older event work must not erase the new one.
        try lane.ledger.acknowledge(
            subscriptionID: "sub-a",
            configGeneration: 1,
            observationEpoch: 1,
            completion: .full(throughRevision: eventRevision)
        )
        XCTAssertEqual(lane.ledger.obligation(subscriptionID: "sub-a")?.fullRevision, scheduled)

        try lane.ledger.acknowledge(
            subscriptionID: "sub-a",
            configGeneration: 1,
            observationEpoch: 1,
            completion: .full(throughRevision: scheduled)
        )
        XCTAssertNil(lane.ledger.obligation(subscriptionID: "sub-a"))
    }

    func testAnEventAfterASchedulePushesTheObligationPastTheScheduledAcknowledgement() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try scheduler.schedule()
        let scheduled = try XCTUnwrap(lane.ledger.obligation(subscriptionID: "sub-a")).revision
        try lane.ledger.ingest(
            FolderObservationBatch(
                identity: FolderObservationStreamIdentity(deviceID: Self.device, streamUUID: Self.uuidA),
                observationEpoch: 1,
                events: [
                    FolderObservationEvent(
                        eventID: 150,
                        subscriptionID: "sub-a",
                        configGeneration: 1,
                        reason: .scope(FolderObservationScope(kind: .subtree, components: ["inner"]))
                    ),
                ]
            )
        )
        try lane.ledger.acknowledge(
            subscriptionID: "sub-a",
            configGeneration: 1,
            observationEpoch: 1,
            completion: .full(throughRevision: scheduled)
        )
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: "sub-a")?.fullRevision)
    }

    func testASubscriptionNoStreamObservesYetIsStillArmedDurably() throws {
        // No `beginStream` ran for this one, so the ledger has nothing to merge into. `beginStream`
        // owes the full reconciliation itself, and the scheduler's own record still demands it.
        let lane = try lane(["sub-a"])
        try lane.store.register(Self.subscription("sub-c"))
        let scheduler = try scheduler(lane)
        let armed = try scheduler.request(subscriptionIDs: ["sub-c"], trigger: .startup)
        XCTAssertEqual(armed.map(\.kind), [.metadata, .content])
        XCTAssertNil(lane.ledger.obligation(subscriptionID: "sub-c"))
        XCTAssertEqual(
            try self.scheduler(lane).coverage(subscriptionID: "sub-c", kind: .metadata).pendingTrigger,
            .startup
        )
    }

    func testACleanlyClosedLedgerFailsTheScheduleInsteadOfArmingIt() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        let before = scheduler.coverage(subscriptionID: "sub-a")
        try lane.ledger.markCleanShutdown()
        lane.clock.advance(15 * 60)

        // A clean checkpoint means the next open demands no catch-up, so an obligation that was
        // never merged would never be re-derived, and an already-armed entry does not retry the
        // merge. Arming on the back of a closed ledger would therefore lose the work.
        XCTAssertThrowsError(try scheduler.schedule()) {
            XCTAssertEqual($0 as? FolderObservationLedgerError, .closed)
        }
        XCTAssertThrowsError(try scheduler.request(subscriptionIDs: ["sub-a"], trigger: .startup)) {
            XCTAssertEqual($0 as? FolderObservationLedgerError, .closed)
        }
        XCTAssertEqual(scheduler.coverage(subscriptionID: "sub-a"), before)
        XCTAssertEqual(
            scheduler.due(),
            [demand("sub-a", .metadata, .due)],
            "the work must stay due rather than look armed"
        )
        // Nothing was armed on disk either.
        XCTAssertEqual(try self.scheduler(lane).coverage(subscriptionID: "sub-a"), before)

        // Reopening gives a writable ledger, and the still-due work now merges its obligation.
        let reopened = try FolderObservationLedger(
            stateDirectory: lane.directory.appendingPathComponent("observation"),
            subscriptions: lane.store,
            gate: lane.gate
        )
        XCTAssertNil(reopened.obligation(subscriptionID: "sub-a"))
        let next = try self.scheduler(lane, observation: reopened)
        XCTAssertEqual(try next.schedule(), [demand("sub-a", .metadata, .due)])
        XCTAssertEqual(next.coverage(subscriptionID: "sub-a", kind: .metadata).pendingTrigger, .due)
        XCTAssertEqual(reopened.obligation(subscriptionID: "sub-a")?.reasons, [.scheduled])
    }

    func testAnEventReasonIsNotSomethingTheSchedulerMayRaise() throws {
        let lane = try lane(["sub-a"])
        for reason in FolderObservationReason.allCases where reason.reportedByEvents {
            XCTAssertThrowsError(
                try lane.ledger.requestFullReconciliation(subscriptionID: "sub-a", reason: reason)
            ) { XCTAssertEqual($0 as? FolderObservationLedgerError, .invalidReason) }
        }
        XCTAssertNil(lane.ledger.obligation(subscriptionID: "sub-a"))
    }

    // MARK: - Failed persistence

    func testAFailedWriteLeavesThePriorDurableStateUnchanged() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        let before = scheduler.coverage(subscriptionID: "sub-a")
        let schedule = lane.directory.appendingPathComponent("schedule")
        // The atomic temp write cannot be created in a read-only directory.
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: schedule.path)

        XCTAssertThrowsError(
            try scheduler.complete(
                subscriptionID: "sub-a",
                kind: .metadata,
                result: FolderReconciliationResult(examined: 99, unreadable: 9)
            )
        ) { XCTAssertEqual($0 as? FolderReconciliationError, .stateUnavailable) }
        XCTAssertEqual(scheduler.coverage(subscriptionID: "sub-a"), before)

        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: schedule.path)
        XCTAssertEqual(try self.scheduler(lane).coverage(subscriptionID: "sub-a"), before)
    }

    func testAFailedWriteWhileArmingLeavesNothingArmed() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        let before = scheduler.coverage(subscriptionID: "sub-a")
        let schedule = lane.directory.appendingPathComponent("schedule")
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: schedule.path)

        XCTAssertThrowsError(try scheduler.request(subscriptionIDs: ["sub-a"], trigger: .wake)) {
            XCTAssertEqual($0 as? FolderReconciliationError, .stateUnavailable)
        }
        XCTAssertEqual(scheduler.coverage(subscriptionID: "sub-a"), before)
        // The ledger obligation did land. Extra work is safe; lost work would not be.
        XCTAssertEqual(ledgerReasons(lane, "sub-a"), [.immediate])

        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: schedule.path)
        XCTAssertEqual(try self.scheduler(lane).coverage(subscriptionID: "sub-a"), before)
    }

    func testTheStateFileIsOwnerOnly() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        let schedule = lane.directory.appendingPathComponent("schedule")
        let file = try FileManager.default.attributesOfItem(
            atPath: schedule.appendingPathComponent("folder-reconciliation-v1.json").path
        )
        XCTAssertEqual((file[.posixPermissions] as? NSNumber)?.intValue, 0o600)
        let directory = try FileManager.default.attributesOfItem(atPath: schedule.path)
        XCTAssertEqual((directory[.posixPermissions] as? NSNumber)?.intValue, 0o700)
    }

    // MARK: - Helpers

    /// Rewrites fields on the first persisted entry, the way a tampered state file would.
    private func tamper(_ lane: Lane, _ fields: [String: Any]) throws {
        let file = lane.directory
            .appendingPathComponent("schedule")
            .appendingPathComponent("folder-reconciliation-v1.json")
        var document = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any]
        )
        var entries = try XCTUnwrap(document["entries"] as? [[String: Any]])
        for (key, value) in fields { entries[0][key] = value }
        document["entries"] = entries
        try JSONSerialization.data(withJSONObject: document).write(to: file)
    }

    private func ledgerReasons(_ lane: Lane, _ id: String) -> [FolderObservationReason] {
        lane.ledger.obligation(subscriptionID: id)?.reasons ?? []
    }
}
