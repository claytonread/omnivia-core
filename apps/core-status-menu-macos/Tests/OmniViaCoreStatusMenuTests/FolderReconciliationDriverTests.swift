@testable import DocumentIntakeMacOS
import Foundation
import XCTest

/// WP-11 slice 4: the lifecycle driver that arms the durable schedule. Every test drives a real
/// `FolderSubscriptionStore`, a real `FolderObservationLedger` and a real
/// `FolderReconciliationScheduler` over a real state directory, so the filter on paused, stopped
/// and not-yet-active subscriptions is proven through the seam that actually owns it.
final class FolderReconciliationDriverTests: XCTestCase {
    /// A clock the test moves by hand. Nothing under test reads the wall clock.
    private final class Clock: @unchecked Sendable {
        private let lock = NSLock()
        private var value: Date

        init(_ value: Date) { self.value = value }

        var now: Date { lock.withLock { value } }

        func advance(_ seconds: TimeInterval) { lock.withLock { value += seconds } }
    }

    private final class Recorder: @unchecked Sendable {
        private let lock = NSLock()
        private var seen: [FolderReconciliationAttention] = []

        var values: [FolderReconciliationAttention] { lock.withLock { seen } }

        var record: @Sendable (FolderReconciliationAttention) -> Void {
            { [self] value in lock.withLock { seen.append(value) } }
        }
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
            .appendingPathComponent("omnivia-reconciliation-driver-" + UUID().uuidString, isDirectory: true)
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
        // obligation is unambiguously the driver's.
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

    private func scheduler(_ lane: Lane) throws -> FolderReconciliationScheduler {
        let clock = lane.clock
        return try FolderReconciliationScheduler(
            stateDirectory: lane.directory.appendingPathComponent("schedule"),
            subscriptions: lane.store,
            observation: lane.ledger,
            clock: { clock.now },
            jitter: { _, _ in 0 },
            gate: lane.gate
        )
    }

    private func driver(
        _ lane: Lane,
        _ scheduler: FolderReconciliationScheduler,
        _ recorder: Recorder = Recorder()
    ) -> (FolderReconciliationDriver, Recorder) {
        (
            FolderReconciliationDriver(
                subscriptions: lane.store,
                scheduler: scheduler,
                onAttention: recorder.record
            ),
            recorder
        )
    }

    // MARK: - Startup

    func testStartupArmsEveryActiveSubscriptionAndKind() throws {
        let lane = try lane()
        let scheduler = try scheduler(lane)
        let (driver, _) = driver(lane, scheduler)

        let demands = driver.startup()

        XCTAssertEqual(
            demands,
            ["sub-a", "sub-b"].flatMap { id in
                FolderReconciliationKind.allCases.map {
                    FolderReconciliationScheduler.Demand(subscriptionID: id, kind: $0, trigger: .startup)
                }
            }
        )
        for id in ["sub-a", "sub-b"] {
            XCTAssertEqual(
                scheduler.coverage(subscriptionID: id).map(\.pendingTrigger),
                [.startup, .startup]
            )
            XCTAssertEqual(ledgerReasons(lane, id), [.immediate])
        }
    }

    /// Startup calls `schedule()` as well as the trigger, so work a previous process armed and
    /// never finished is surfaced instead of waiting for the first periodic tick.
    func testStartupSurfacesWorkAPreviousProcessLeftArmed() throws {
        let lane = try lane(["sub-a"])
        try scheduler(lane).request(subscriptionIDs: ["sub-a"], trigger: .wake)

        let restarted = try scheduler(lane)
        let (driver, recorder) = driver(lane, restarted)
        let demands = driver.startup()

        // The persisted `.wake` is still outstanding, so `.startup` merges nothing on top of it.
        XCTAssertEqual(
            demands,
            FolderReconciliationKind.allCases.map {
                FolderReconciliationScheduler.Demand(subscriptionID: "sub-a", kind: $0, trigger: .wake)
            }
        )
        XCTAssertEqual(recorder.values, [])
    }

    func testStartupSurfacesAKindWhoseIntervalElapsedWhileTheProcessWasClosed() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        lane.clock.advance(FolderReconciliationKind.metadata.interval)

        let restarted = try self.scheduler(lane)
        let (driver, _) = driver(lane, restarted)
        // `.startup` claims both kinds first; the due metadata interval is covered by that same
        // outstanding obligation rather than armed twice.
        XCTAssertEqual(
            driver.startup(),
            FolderReconciliationKind.allCases.map {
                FolderReconciliationScheduler.Demand(subscriptionID: "sub-a", kind: $0, trigger: .startup)
            }
        )
    }

    // MARK: - Due tick

    func testATickArmsNothingUntilAnIntervalElapses() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        let (driver, recorder) = driver(lane, scheduler)

        lane.clock.advance(FolderReconciliationKind.metadata.interval - 1)
        XCTAssertEqual(driver.tick(), [])

        lane.clock.advance(1)
        XCTAssertEqual(
            driver.tick(),
            [FolderReconciliationScheduler.Demand(subscriptionID: "sub-a", kind: .metadata, trigger: .due)]
        )
        XCTAssertEqual(ledgerReasons(lane, "sub-a"), [.scheduled])
        XCTAssertEqual(recorder.values, [])
    }

    /// The app timer must be shorter than the shortest interval the schedule owns, jitter and all,
    /// or a due instant waits for the next tick rather than for itself.
    func testTheTickIntervalCannotStretchTheMetadataInterval() {
        let jittered = FolderReconciliationKind.metadata.interval
            * (1 + FolderReconciliationScheduler.maximumJitterFraction)
        XCTAssertEqual(jittered, 18 * 60)
        // The worst case a tick can add to a due instant is one whole tick period, so the longest
        // a 15-minute interval can be stretched to stays well under half an hour.
        XCTAssertLessThan(jittered + FolderReconciliationDriver.tickInterval, 30 * 60)
        XCTAssertEqual(FolderReconciliationDriver.tickInterval, 60)
    }

    // MARK: - Wake and remount

    func testWakeArmsEveryActiveSubscription() throws {
        let lane = try lane()
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        try settle(scheduler, "sub-b")
        let (driver, recorder) = driver(lane, scheduler)

        XCTAssertEqual(
            driver.wake(),
            ["sub-a", "sub-b"].flatMap { id in
                FolderReconciliationKind.allCases.map {
                    FolderReconciliationScheduler.Demand(subscriptionID: id, kind: $0, trigger: .wake)
                }
            }
        )
        XCTAssertEqual(ledgerReasons(lane, "sub-a"), [.immediate])
        XCTAssertEqual(recorder.values, [])
    }

    func testRemountArmsEveryActiveSubscription() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        let (driver, _) = driver(lane, scheduler)

        XCTAssertEqual(
            driver.remount(),
            FolderReconciliationKind.allCases.map {
                FolderReconciliationScheduler.Demand(subscriptionID: "sub-a", kind: $0, trigger: .remount)
            }
        )
        XCTAssertEqual(
            scheduler.coverage(subscriptionID: "sub-a").map(\.pendingTrigger),
            [.remount, .remount]
        )
    }

    // MARK: - Filtering

    func testNoSubscriptionOutsideActiveIsEverArmed() throws {
        let states: [FolderSubscriptionState] = [.waitingForCore, .paused, .reviewRequired, .needsAccess, .stopped]
        let ids = states.map { "sub-\($0.rawValue.replacingOccurrences(of: "_", with: "-"))" }
        let lane = try lane(ids + ["sub-active"])
        for (id, state) in zip(ids, states) { try lane.store.update(id: id) { $0.state = state } }
        let scheduler = try scheduler(lane)
        let (driver, recorder) = driver(lane, scheduler)

        XCTAssertEqual(Set(driver.startup().map(\.subscriptionID)), ["sub-active"])
        // The rest find the one active subscription already armed, so they add nothing at all.
        XCTAssertEqual(driver.wake(), [])
        XCTAssertEqual(driver.remount(), [])
        XCTAssertEqual(Set(driver.tick().map(\.subscriptionID)), ["sub-active"])
        for id in ids {
            XCTAssertEqual(scheduler.coverage(subscriptionID: id).map(\.pendingTrigger), [nil, nil])
            XCTAssertEqual(ledgerReasons(lane, id), [])
        }
        XCTAssertEqual(recorder.values, [])
    }

    /// Pausing after a trigger was armed does not withdraw it — but nothing new is armed either,
    /// and a paused subscription is never surfaced by a tick.
    func testPausingAfterArmingStopsFurtherArming() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        let (driver, _) = driver(lane, scheduler)
        driver.startup()
        try lane.store.update(id: "sub-a") { $0.state = .paused }

        XCTAssertEqual(driver.wake(), [])
        XCTAssertEqual(driver.tick(), [])
        lane.clock.advance(FolderReconciliationKind.content.interval * 2)
        XCTAssertEqual(driver.tick(), [])
    }

    // MARK: - Idempotence

    func testRepeatedTriggersArmNothingTwice() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        let (driver, recorder) = driver(lane, scheduler)

        let first = driver.startup()
        XCTAssertEqual(driver.startup(), first)
        XCTAssertEqual(driver.wake(), [])
        XCTAssertEqual(driver.remount(), [])
        // One obligation, not four: the outstanding full reconciliation covers every repeat.
        XCTAssertEqual(ledgerReasons(lane, "sub-a"), [.immediate])
        XCTAssertEqual(
            scheduler.coverage(subscriptionID: "sub-a").map(\.pendingTrigger),
            [.startup, .startup]
        )
        XCTAssertEqual(recorder.values, [])
    }

    func testRepeatedTicksWithNothingDueChangeNothing() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        let (driver, _) = driver(lane, scheduler)
        let before = scheduler.coverage(subscriptionID: "sub-a")

        for _ in 0..<5 { XCTAssertEqual(driver.tick(), []) }

        XCTAssertEqual(scheduler.coverage(subscriptionID: "sub-a"), before)
        XCTAssertEqual(ledgerReasons(lane, "sub-a"), [])
    }

    // MARK: - Errors

    func testATriggerThatCannotBeArmedIsReportedAndNotTreatedAsDone() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        let (driver, recorder) = driver(lane, scheduler)
        let before = scheduler.coverage(subscriptionID: "sub-a")
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o500],
            ofItemAtPath: lane.directory.appendingPathComponent("schedule").path
        )

        XCTAssertEqual(driver.wake(), [])

        XCTAssertEqual(recorder.values, [.trigger(.wake)])
        XCTAssertEqual(driver.attention, [.trigger(.wake)])
        XCTAssertEqual(scheduler.coverage(subscriptionID: "sub-a"), before)
    }

    func testADueTickThatCannotBeArmedIsReportedAndNotTreatedAsDone() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        try settle(scheduler, "sub-a")
        let (driver, recorder) = driver(lane, scheduler)
        let before = scheduler.coverage(subscriptionID: "sub-a")
        lane.clock.advance(FolderReconciliationKind.metadata.interval)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o500],
            ofItemAtPath: lane.directory.appendingPathComponent("schedule").path
        )

        XCTAssertEqual(driver.tick(), [])

        XCTAssertEqual(recorder.values, [.schedule])
        XCTAssertEqual(driver.attention, [.schedule])
        XCTAssertEqual(scheduler.coverage(subscriptionID: "sub-a"), before)
    }

    /// Startup reports the failed trigger and still ticks: surfacing due work is the conservative
    /// half of the pair, so it is not skipped because the other half failed.
    func testStartupStillTicksAfterItsTriggerFailed() throws {
        let lane = try lane(["sub-a"])
        let scheduler = try scheduler(lane)
        let (driver, recorder) = driver(lane, scheduler)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o500],
            ofItemAtPath: lane.directory.appendingPathComponent("schedule").path
        )

        XCTAssertEqual(driver.startup(), [])

        XCTAssertEqual(recorder.values, [.trigger(.startup), .schedule])
    }

    func testNoSubscriptionsMeansNoAttentionAndNoWrite() throws {
        let lane = try lane([])
        let scheduler = try scheduler(lane)
        let (driver, recorder) = driver(lane, scheduler)

        XCTAssertEqual(driver.startup(), [])
        XCTAssertEqual(driver.wake(), [])
        XCTAssertEqual(driver.remount(), [])
        XCTAssertEqual(recorder.values, [])
    }

    // MARK: - Helpers

    private func ledgerReasons(_ lane: Lane, _ id: String) -> [FolderObservationReason] {
        lane.ledger.obligation(subscriptionID: id)?.reasons ?? []
    }

    /// Clears everything armed for one subscription, with complete coverage.
    private func settle(_ scheduler: FolderReconciliationScheduler, _ id: String) throws {
        for kind in FolderReconciliationKind.allCases {
            try scheduler.complete(
                subscriptionID: id,
                kind: kind,
                result: FolderReconciliationResult(examined: 3)
            )
        }
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
}
