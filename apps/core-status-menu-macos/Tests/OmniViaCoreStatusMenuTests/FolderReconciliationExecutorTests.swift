import CryptoKit
@testable import DocumentIntakeMacOS
import Foundation
import XCTest

/// WP-11 slice 5: the executor that runs the demands the schedule armed.
///
/// Every test drives the real stores — `NativeAccessStore`, `FolderSubscriptionStore`,
/// `FolderObservationLedger`, `FolderReconciliationScheduler` — over a real folder on disk, so
/// what is proven here is the behaviour of the seams that actually own each rule. Only three
/// things are injected: the clock, the bookmark codec (which doubles as the one deterministic
/// mid-pass hook) and the residency probe, because provider residency cannot be produced on a
/// test volume.
final class FolderReconciliationExecutorTests: XCTestCase {
    /// A clock the test moves by hand. Nothing under test reads the wall clock.
    private final class Clock: @unchecked Sendable {
        private let lock = NSLock()
        private var value: Date

        init(_ value: Date) { self.value = value }

        var now: Date { lock.withLock { value } }

        func advance(_ seconds: TimeInterval) { lock.withLock { value += seconds } }
    }

    /// Path-encoded bookmarks, plus the two things the tests need from the grant layer: a stale
    /// route, and a hook that runs inside the scope access a pass holds. The hook is how a race
    /// is injected deterministically — it fires after the executor captured the revision and
    /// epoch and before the pass has reported anything.
    private final class Codec: SecurityScopedBookmarkCoding, @unchecked Sendable {
        private let lock = NSLock()
        private var open = 0
        private var peak = 0
        private var opened = 0
        private var staleRoute = false
        private var hook: (@Sendable () -> Void)?

        /// How many times a grant's scope was entered: one per metadata scan, one per member read.
        var accesses: Int { lock.withLock { opened } }
        /// The highest number of scopes held at once. Above one would mean two passes overlapped.
        var peakConcurrentAccesses: Int { lock.withLock { peak } }

        func makeStale() { lock.withLock { staleRoute = true } }

        func onScopeEntered(_ value: @escaping @Sendable () -> Void) {
            lock.withLock { hook = value }
        }

        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }

        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            (
                URL(fileURLWithPath: String(decoding: data, as: UTF8.self)),
                lock.withLock { staleRoute }
            )
        }

        func startAccessing(_ url: URL) -> Bool {
            let fire: (@Sendable () -> Void)? = lock.withLock {
                opened += 1
                open += 1
                peak = max(peak, open)
                let value = hook
                hook = nil
                return value
            }
            fire?()
            return true
        }

        func stopAccessing(_ url: URL) { lock.withLock { open -= 1 } }
    }

    /// Records every content read, so a pass that claims to read no bytes can be held to it.
    private final class CountingReader: CoordinatedOriginalReading, @unchecked Sendable {
        private let lock = NSLock()
        private var seen: [URL] = []

        var reads: [URL] { lock.withLock { seen } }

        func readStableBytes(from url: URL, maximumBytes: UInt64) throws -> Data {
            lock.withLock { seen.append(url) }
            return try Data(contentsOf: url)
        }
    }

    private struct Lane {
        let root: URL
        let state: URL
        let gate: PublicationGate
        let accessStore: NativeAccessStore
        let store: FolderSubscriptionStore
        let ledger: FolderObservationLedger
        let scheduler: FolderReconciliationScheduler
        let codec: Codec
        let reader: CountingReader
        let clock: Clock
        let rootGeneration: UInt64
        let observationEpoch: UInt64
    }

    private static let id = "sub-a"
    private static let device: Int64 = 16_777_230
    private static let uuidA = "11111111-1111-4111-8111-111111111111"
    private static let uuidB = "22222222-2222-4222-8222-222222222222"
    private static let epoch = Date(timeIntervalSince1970: 1_760_000_000)

    // MARK: - Metadata

    func testAMetadataPassEnumeratesTheAuthorisedScopeAndReadsNoContentBytes() async throws {
        let lane = try lane(files: ["a.txt": "one", "nested/b.txt": "two", ".hidden.txt": "three"])
        try arm(lane)
        try record(lane, members: ["a.txt", "nested/b.txt"])
        let executor = executor(lane)

        let runs = await executor.run(demands(lane, .metadata))

        // The two approved files, recursively; the hidden one is excluded rather than uncovered.
        XCTAssertEqual(runs.map(\.outcome), [.complete])
        // Two covered and nothing left uncovered, which is what makes the outcome complete.
        XCTAssertEqual(runs.first?.result, FolderReconciliationResult(examined: 2))
        // The whole point of the cheap pass: membership and metadata, and not one content byte.
        XCTAssertEqual(lane.reader.reads, [])
        XCTAssertEqual(lane.codec.accesses, 1)
        let coverage = lane.scheduler.coverage(subscriptionID: Self.id, kind: .metadata)
        XCTAssertEqual(coverage.lastOutcome, .complete)
        XCTAssertNil(coverage.pendingTrigger)
        XCTAssertTrue(coverage.isUpToDate(at: lane.clock.now))
    }

    func testAMetadataPassReadsOnlyWhatTheSubscriptionsOwnScopeApproved() async throws {
        let lane = try lane(
            files: ["a.txt": "one", "nested/b.txt": "two"],
            recursive: false
        )
        try arm(lane)
        try record(lane, members: ["a.txt"])

        let runs = await executor(lane).run(demands(lane, .metadata))

        // Recursion is off, so the subfolder is never listed: one member, complete coverage.
        XCTAssertEqual(runs.first?.result?.examined, 1)
        XCTAssertEqual(runs.map(\.outcome), [.complete])
        XCTAssertEqual(lane.reader.reads, [])
    }

    func testAScanThatReachesItsBoundIsReportedAsAPassThatDidNotFinish() async throws {
        let lane = try lane(files: ["a.txt": "one", "b.txt": "two", "c.txt": "three"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        let executor = executor(
            lane,
            limits: FolderScopeLimits(maximumEligibleFiles: 1, maximumExaminedEntries: 100, maximumDepth: 4)
        )

        let runs = await executor.run(demands(lane, .metadata))

        // A prefix of the folder is not the folder: nothing here may be read as coverage.
        XCTAssertEqual(runs.map(\.outcome), [.failed])
        XCTAssertFalse(try XCTUnwrap(runs.first).acknowledged)
        XCTAssertFalse(try XCTUnwrap(runs.first).drifted)
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: Self.id)?.fullRevision)
        XCTAssertNil(lane.scheduler.coverage(subscriptionID: Self.id, kind: .metadata).lastCompleteAt)
    }

    func testAnExaminedEntryBoundIsAlsoAPassThatDidNotFinish() async throws {
        let lane = try lane(files: ["a.txt": "one", "b.txt": "two", "c.txt": "three"])
        try arm(lane)
        try record(lane, members: [])
        let executor = executor(
            lane,
            limits: FolderScopeLimits(maximumEligibleFiles: 10, maximumExaminedEntries: 2, maximumDepth: 4)
        )

        let runs = await executor.run(demands(lane, .metadata))

        XCTAssertEqual(runs.map(\.outcome), [.failed])
    }

    func testARootWhoseGrantWillNotResolveIsReportedAsAnUnavailableProvider() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        lane.codec.makeStale()

        let runs = await executor(lane).run(demands(lane, .metadata))

        XCTAssertEqual(runs.map(\.outcome), [.providerUnavailable])
        XCTAssertEqual(runs.first?.result?.examined, 0)
        XCTAssertFalse(try XCTUnwrap(runs.first).acknowledged)
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: Self.id)?.fullRevision)
    }

    func testAPartialScanNeitherAcknowledgesNorTouchesThePriorInventory() async throws {
        let lane = try lane(files: ["a.txt": "one", "locked/b.txt": "two"])
        try arm(lane)
        // Recorded while the subtree is still readable: what is under test is a pass that cannot
        // see a member it already has, not a member that could never be enrolled.
        try record(lane, members: ["a.txt", "locked/b.txt"])
        // A subtree that cannot be listed is exactly what a deleted subtree looks like from here.
        let locked = lane.root.appendingPathComponent("locked", isDirectory: true)
        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: locked.path)
        addTeardownBlock {
            try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: locked.path)
        }
        let before = try XCTUnwrap(lane.store.subscription(id: Self.id))

        let runs = await executor(lane).run(demands(lane, .metadata))

        XCTAssertEqual(runs.map(\.outcome), [.partial])
        XCTAssertEqual(runs.first?.result?.unreadable, 1)
        XCTAssertFalse(try XCTUnwrap(runs.first).acknowledged)
        // Nothing is inferred from what the pass could not see: no member is marked absent, no
        // recorded digest is rewritten, and the capture path is not sent after phantom work.
        XCTAssertEqual(lane.store.subscription(id: Self.id), before)
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: Self.id)?.fullRevision)
    }

    func testAMetadataPassThatSeesDriftHandsTheWorkToTheCapturePath() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        // Recorded at a byte length the file no longer has.
        try record(lane, members: ["a.txt"], byteLength: 99)
        let before = try XCTUnwrap(lane.store.subscription(id: Self.id)).members

        let runs = await executor(lane).run(demands(lane, .metadata))

        XCTAssertEqual(runs.map(\.outcome), [.complete])
        XCTAssertTrue(try XCTUnwrap(runs.first).drifted)
        let after = try XCTUnwrap(lane.store.subscription(id: Self.id))
        XCTAssertTrue(after.needsFullReconciliation)
        // The flag is the whole handoff: this executor rewrites no member and queues nothing.
        XCTAssertEqual(after.members, before)
        XCTAssertEqual(lane.reader.reads, [])
    }

    func testAMetadataPassOverAnUnchangedFolderReportsNoDrift() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])

        let runs = await executor(lane).run(demands(lane, .metadata))

        XCTAssertFalse(try XCTUnwrap(runs.first).drifted)
        XCTAssertFalse(try XCTUnwrap(lane.store.subscription(id: Self.id)).needsFullReconciliation)
    }

    // MARK: - Content

    func testAContentPassDigestsResidentMembersOnlyAndEnumeratesNothing() async throws {
        let lane = try lane(files: ["a.txt": "one", "b.txt": "two", "c.txt": "three", "d.txt": "four"])
        try arm(lane)
        try record(lane, members: ["a.txt", "b.txt", "c.txt"])
        // `d.txt` is recorded with no digest at all: its baseline is the capture path's
        // outstanding work, so it is not part of this pass's population.
        try lane.store.update(id: Self.id) { value in
            value.members.append(try self.member(lane, "d.txt", digest: nil))
        }
        let executor = executor(lane) { url in
            switch url.lastPathComponent {
            case "b.txt": .notResident
            case "c.txt": .unreadable
            default: .resident
            }
        }

        let runs = await executor.run(demands(lane, .content))

        XCTAssertEqual(
            runs.first?.result,
            FolderReconciliationResult(examined: 1, notResident: 1, unreadable: 1)
        )
        XCTAssertEqual(runs.map(\.outcome), [.partial])
        // Only the resident member's bytes were read: residency is qualified before anything is
        // opened, so a provider-only member is never hydrated to close the gap.
        XCTAssertEqual(lane.reader.reads.map(\.lastPathComponent), ["a.txt"])
        XCTAssertEqual(lane.scheduler.coverage(subscriptionID: Self.id, kind: .content).notResident, 1)
    }

    func testACompleteContentPassNeverAcknowledgesTheMembershipObligation() async throws {
        let lane = try lane(files: ["a.txt": "one", "b.txt": "two"])
        try arm(lane)
        try record(lane, members: ["a.txt", "b.txt"])

        let runs = await executor(lane).run(demands(lane, .content))

        XCTAssertEqual(runs.map(\.outcome), [.complete])
        XCTAssertEqual(runs.first?.result?.examined, 2)
        // Content re-verifies recorded members; it never re-reads membership, so it can never be
        // the full reconciliation the obligation asks for.
        XCTAssertFalse(try XCTUnwrap(runs.first).acknowledged)
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: Self.id)?.fullRevision)
        XCTAssertEqual(lane.codec.accesses, 2)
    }

    func testContentDriftSetsTheFlagWithoutRewritingTheRecordedMember() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        try Data("changed".utf8).write(to: lane.root.appendingPathComponent("a.txt"))
        let before = try XCTUnwrap(lane.store.subscription(id: Self.id)).members

        let runs = await executor(lane).run(demands(lane, .content))

        XCTAssertTrue(try XCTUnwrap(runs.first).drifted)
        XCTAssertEqual(runs.map(\.outcome), [.complete])
        let after = try XCTUnwrap(lane.store.subscription(id: Self.id))
        XCTAssertTrue(after.needsFullReconciliation)
        XCTAssertEqual(after.members, before)
    }

    func testAMemberGrantThatWillNotResolveIsUnreadableRatherThanAbsent() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        // A member whose frozen grant generation no longer matches: no bytes, and no evidence.
        // The generation is frozen on the recorded member, so the member is replaced rather than
        // mutated — which is also what a real re-adoption would produce.
        let mismatched = try member(lane, "a.txt", generationOffset: 1)
        try lane.store.update(id: Self.id) { $0.members = [mismatched] }
        let before = try XCTUnwrap(lane.store.subscription(id: Self.id)).members

        let runs = await executor(lane).run(demands(lane, .content))

        XCTAssertEqual(runs.first?.result, FolderReconciliationResult(examined: 0, unreadable: 1))
        XCTAssertEqual(runs.map(\.outcome), [.partial])
        XCTAssertEqual(lane.reader.reads, [])
        XCTAssertEqual(try XCTUnwrap(lane.store.subscription(id: Self.id)).members, before)
    }

    func testAnUnavailableProviderDuringAContentPassReportsThatNothingWasCovered() async throws {
        let lane = try lane(files: ["a.txt": "one", "b.txt": "two"])
        try arm(lane)
        try record(lane, members: ["a.txt", "b.txt"])

        let runs = await executor(lane) { _ in .providerUnavailable }.run(demands(lane, .content))

        XCTAssertEqual(
            runs.first?.result,
            FolderReconciliationResult(examined: 0, providerAvailable: false)
        )
        XCTAssertEqual(runs.map(\.outcome), [.providerUnavailable])
        XCTAssertEqual(lane.reader.reads, [])
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: Self.id)?.fullRevision)
    }

    // MARK: - Acknowledgement

    func testACompleteMetadataPassAcknowledgesTheObligationItCaptured() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        let revision = try XCTUnwrap(lane.ledger.obligation(subscriptionID: Self.id)).revision

        let runs = await executor(lane).run(demands(lane, .metadata))

        XCTAssertTrue(try XCTUnwrap(runs.first).acknowledged)
        XCTAssertNil(lane.ledger.obligation(subscriptionID: Self.id))
        XCTAssertGreaterThan(revision, 0)
    }

    /// The revision is captured before the scan, so an obligation merged while the pass was
    /// reading takes a higher one and survives the acknowledgement. Nothing the pass did not
    /// cover is erased by what it did.
    func testAnObligationMergedDuringAPassSurvivesTheAcknowledgement() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        let captured = try XCTUnwrap(lane.ledger.obligation(subscriptionID: Self.id)).revision
        let ledger = lane.ledger
        lane.codec.onScopeEntered {
            _ = try? ledger.ingest(
                FolderObservationBatch(
                    identity: FolderObservationStreamIdentity(
                        deviceID: Self.device,
                        streamUUID: Self.uuidA
                    ),
                    observationEpoch: lane.observationEpoch,
                    events: [
                        FolderObservationEvent(
                            eventID: 200,
                            subscriptionID: Self.id,
                            configGeneration: 1,
                            reason: .full(.eventsDropped)
                        ),
                    ]
                )
            )
        }

        let runs = await executor(lane).run(demands(lane, .metadata))

        XCTAssertTrue(try XCTUnwrap(runs.first).acknowledged)
        let outstanding = try XCTUnwrap(lane.ledger.obligation(subscriptionID: Self.id))
        XCTAssertGreaterThan(outstanding.revision, captured)
        XCTAssertTrue(outstanding.reasons.contains(.eventsDropped))
    }

    // MARK: - Races

    /// The epoch is re-read after the scan. A stream whose identity changed under the pass has no
    /// comparable history, so what the pass read is not the work that is owed now: the schedule is
    /// not told anything finished, and the demand stays armed.
    func testAnObservationEpochChangeDuringAPassIsNeitherAcknowledgedNorReported() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        let ledger = lane.ledger
        lane.codec.onScopeEntered {
            // A new stream UUID on the same device: a new observation epoch.
            _ = try? ledger.beginStream(
                identity: FolderObservationStreamIdentity(
                    deviceID: Self.device,
                    streamUUID: Self.uuidB
                ),
                startEventID: 10,
                subscriptions: [
                    FolderObservationApplicableSubscription(subscriptionID: Self.id, configGeneration: 1),
                ]
            )
        }

        let runs = await executor(lane).run(demands(lane, .metadata))

        XCTAssertEqual(runs.map(\.skipped), [.raced])
        XCTAssertNil(runs.first?.result)
        XCTAssertFalse(try XCTUnwrap(runs.first).acknowledged)
        // Neither half of a completion landed: no coverage truth, and the demand is still armed.
        let coverage = lane.scheduler.coverage(subscriptionID: Self.id, kind: .metadata)
        XCTAssertNil(coverage.lastOutcome)
        XCTAssertNil(coverage.lastAttemptAt)
        XCTAssertEqual(coverage.pendingTrigger, .startup)
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: Self.id)?.fullRevision)
        XCTAssertNotEqual(lane.ledger.streams().first?.observationEpoch, lane.observationEpoch)
    }

    func testAStateChangeDuringAPassIsNeitherAcknowledgedNorReported() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        let store = lane.store
        lane.codec.onScopeEntered { _ = try? store.update(id: Self.id) { $0.state = .paused } }

        let runs = await executor(lane).run(demands(lane, .metadata))

        XCTAssertEqual(runs.map(\.skipped), [.raced])
        XCTAssertNil(lane.scheduler.coverage(subscriptionID: Self.id, kind: .metadata).lastOutcome)
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: Self.id)?.fullRevision)
    }

    func testAnObservationEpochChangeDuringAContentPassIsAlsoRefused() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        let ledger = lane.ledger
        lane.codec.onScopeEntered {
            _ = try? ledger.beginStream(
                identity: FolderObservationStreamIdentity(
                    deviceID: Self.device,
                    streamUUID: Self.uuidB
                ),
                startEventID: 10,
                subscriptions: [
                    FolderObservationApplicableSubscription(subscriptionID: Self.id, configGeneration: 1),
                ]
            )
        }

        let runs = await executor(lane).run(demands(lane, .content))

        XCTAssertEqual(runs.map(\.skipped), [.raced])
        XCTAssertNil(lane.scheduler.coverage(subscriptionID: Self.id, kind: .content).lastOutcome)
    }

    // MARK: - The finalization window

    /// The narrower window the mid-pass tests cannot reach: the pass has finished reading and
    /// nothing has been committed yet. Revalidating here and then acknowledging and completing as
    /// two separate steps would let a pause land between them, so the recheck and both writes are
    /// one critical section on the gate every one of those mutations also takes.
    func testAStateChangeInTheFinalizationWindowCommitsNeitherHalf() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        let store = lane.store
        let executor = executor(lane)
        await executor.onFinalization { _ = try? store.update(id: Self.id) { $0.state = .paused } }

        let runs = await executor.run(demands(lane, .metadata))

        XCTAssertEqual(runs.map(\.skipped), [.raced])
        XCTAssertNil(runs.first?.result)
        XCTAssertFalse(try XCTUnwrap(runs.first).acknowledged)
        // Neither half landed: the obligation is outstanding and the demand is still armed.
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: Self.id)?.fullRevision)
        let coverage = lane.scheduler.coverage(subscriptionID: Self.id, kind: .metadata)
        XCTAssertNil(coverage.lastOutcome)
        XCTAssertNil(coverage.lastAttemptAt)
        XCTAssertEqual(coverage.pendingTrigger, .startup)
    }

    /// A content pass acknowledges nothing, so the only thing it can wrongly commit is the
    /// completion — which is what clears the armed demand.
    func testAnEpochChangeInTheFinalizationWindowLeavesTheContentDemandArmed() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        let ledger = lane.ledger
        let executor = executor(lane)
        await executor.onFinalization {
            _ = try? ledger.beginStream(
                identity: FolderObservationStreamIdentity(
                    deviceID: Self.device,
                    streamUUID: Self.uuidB
                ),
                startEventID: 10,
                subscriptions: [
                    FolderObservationApplicableSubscription(subscriptionID: Self.id, configGeneration: 1),
                ]
            )
        }

        let runs = await executor.run(demands(lane, .content))

        XCTAssertEqual(runs.map(\.skipped), [.raced])
        let coverage = lane.scheduler.coverage(subscriptionID: Self.id, kind: .content)
        XCTAssertNil(coverage.lastOutcome)
        XCTAssertEqual(coverage.pendingTrigger, .startup)
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: Self.id)?.fullRevision)
    }

    /// The next write in the same critical section. An acknowledgement the ledger refuses must not
    /// be followed by a completion: a schedule that recorded coverage here would be reporting work
    /// whose obligation is still outstanding.
    func testAnAcknowledgementTheLedgerRefusesLeavesTheScheduleUntouched() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        let ledger = lane.ledger
        let executor = executor(lane)
        // A ledger checkpointed clean records nothing further, so the acknowledgement is refused
        // while every guard the pass captured still holds.
        await executor.onFinalization { try? ledger.markCleanShutdown() }

        let runs = await executor.run(demands(lane, .metadata))

        XCTAssertEqual(runs.map(\.skipped), [.raced])
        XCTAssertFalse(try XCTUnwrap(runs.first).acknowledged)
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: Self.id)?.fullRevision)
        let coverage = lane.scheduler.coverage(subscriptionID: Self.id, kind: .metadata)
        XCTAssertNil(coverage.lastOutcome)
        XCTAssertNil(coverage.lastAttemptAt)
        XCTAssertEqual(coverage.pendingTrigger, .startup)
    }

    /// The drift flag is the whole handoff to the capture path, so a pass that cannot persist it
    /// must not report coverage either: completing the schedule here would clear the armed demand
    /// and leave the drift this pass saw recorded nowhere at all.
    func testADriftHandoffTheStoreRefusesLeavesTheDemandArmed() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        // Recorded at a byte length the file no longer has: the drift the pass will see.
        try record(lane, members: ["a.txt"], byteLength: 99)
        let subscriptions = lane.state.appendingPathComponent("subscriptions", isDirectory: true)
        addTeardownBlock {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o700],
                ofItemAtPath: subscriptions.path
            )
        }
        let executor = executor(lane)
        // The store cannot write from here on, while every guard the pass captured still holds.
        await executor.onFinalization {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o500],
                ofItemAtPath: subscriptions.path
            )
        }

        let runs = await executor.run(demands(lane, .metadata))

        XCTAssertEqual(runs.map(\.skipped), [.raced])
        XCTAssertNil(runs.first?.result)
        XCTAssertTrue(try XCTUnwrap(runs.first).drifted)
        XCTAssertFalse(try XCTUnwrap(runs.first).acknowledged)
        XCTAssertFalse(try XCTUnwrap(lane.store.subscription(id: Self.id)).needsFullReconciliation)
        // Nothing was acknowledged, no coverage was recorded, and the demand is still owed.
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: Self.id)?.fullRevision)
        let coverage = lane.scheduler.coverage(subscriptionID: Self.id, kind: .metadata)
        XCTAssertNil(coverage.lastOutcome)
        XCTAssertNil(coverage.lastAttemptAt)
        XCTAssertEqual(coverage.pendingTrigger, .startup)
        XCTAssertEqual(
            lane.scheduler.due().filter { $0.kind == .metadata }.map(\.subscriptionID),
            [Self.id]
        )
    }

    // MARK: - Gating

    func testNoSubscriptionOutsideActiveIsEverRead() async throws {
        for state in [
            FolderSubscriptionState.paused, .stopped, .reviewRequired, .needsAccess, .waitingForCore,
        ] {
            let lane = try lane(files: ["a.txt": "one"])
            try arm(lane)
            try record(lane, members: ["a.txt"])
            let armed = demands(lane, .metadata) + demands(lane, .content)
            try lane.store.update(id: Self.id) { $0.state = state }

            let runs = await executor(lane).run(armed)

            XCTAssertEqual(runs.map(\.skipped), [.notActive, .notActive], "\(state)")
            XCTAssertEqual(lane.codec.accesses, 0, "\(state)")
            XCTAssertEqual(lane.reader.reads, [], "\(state)")
            XCTAssertNil(lane.scheduler.coverage(subscriptionID: Self.id, kind: .metadata).lastOutcome)
        }
    }

    func testARootWhoseEstablishmentNeverFinishedIsNeverRead() async throws {
        let lane = try lane(files: ["a.txt": "one"], activateRoot: false)
        try arm(lane)
        try record(lane, members: ["a.txt"])

        let runs = await executor(lane).run(demands(lane, .metadata) + demands(lane, .content))

        XCTAssertEqual(runs.map(\.skipped), [.notEstablished, .notEstablished])
        XCTAssertEqual(lane.codec.accesses, 0)
        XCTAssertNil(lane.scheduler.coverage(subscriptionID: Self.id, kind: .metadata).lastAttemptAt)
    }

    func testAnUnknownSubscriptionIsSkipped() async throws {
        let lane = try lane(files: ["a.txt": "one"])

        let runs = await executor(lane).run([
            FolderReconciliationScheduler.Demand(
                subscriptionID: "sub-missing",
                kind: .metadata,
                trigger: .startup
            ),
        ])

        XCTAssertEqual(runs.map(\.skipped), [.unknownSubscription])
        XCTAssertEqual(lane.codec.accesses, 0)
    }

    // MARK: - Idempotence

    func testTheSameDemandHandedOverTwiceIsOnePass() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        let executor = executor(lane)
        let armed = demands(lane, .metadata)

        let first = await executor.run(armed)
        let second = await executor.run(armed)

        XCTAssertEqual(first.map(\.outcome), [.complete])
        // The schedule is the one owner of what is still owed, and it no longer owes this.
        XCTAssertEqual(second.map(\.skipped), [.notOwed])
        XCTAssertEqual(lane.codec.accesses, 1)
    }

    func testOverlappingBatchesNeitherRunTogetherNorRunTheSameDemandTwice() async throws {
        let lane = try lane(files: ["a.txt": "one", "b.txt": "two"])
        try arm(lane)
        try record(lane, members: ["a.txt", "b.txt"])
        let executor = executor(lane)
        let armed = demands(lane, .metadata) + demands(lane, .content)

        let batches = await withTaskGroup(of: [FolderReconciliationRun].self) { group in
            for _ in 0..<4 { group.addTask { await executor.run(armed) } }
            var all: [[FolderReconciliationRun]] = []
            for await value in group { all.append(value) }
            return all
        }

        // One scope at a time, whichever order the batches arrived in: the actor is the whole of
        // the guarantee.
        XCTAssertEqual(lane.codec.peakConcurrentAccesses, 1)
        let ran = batches.flatMap { $0 }.filter { $0.result != nil }
        XCTAssertEqual(ran.count, 2)
        XCTAssertEqual(Set(ran.map(\.kind)), Set(FolderReconciliationKind.allCases))
        XCTAssertEqual(
            batches.flatMap { $0 }.filter { $0.skipped == .notOwed }.count,
            armed.count * 4 - 2
        )
        // The whole folder was enumerated once and each member digested once.
        XCTAssertEqual(lane.reader.reads.count, 2)
    }

    // MARK: - Bounds

    /// Coverage failures are not themselves bounded by the eligible-file limit, so the pass clamps
    /// its counts to something the schedule can record — and clamping only ever shrinks what it
    /// claims to have covered.
    func testCountsAreClampedToWhatTheScheduleCanRecord() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        let limit = FolderReconciliationScheduler.maximumCount

        for result in [
            FolderReconciliationExecutor.bounded(examined: .max, unreadable: .max, notResident: .max, failed: false),
            FolderReconciliationExecutor.bounded(examined: limit, unreadable: limit, failed: true),
            FolderReconciliationExecutor.bounded(examined: 3, unreadable: 1, notResident: 2, failed: false),
        ] {
            XCTAssertTrue(
                FolderReconciliationScheduler.withinEligibleBound(
                    result.examined,
                    result.notResident,
                    result.unreadable
                ),
                "\(result)"
            )
            XCTAssertNoThrow(
                try lane.scheduler.complete(
                    subscriptionID: Self.id,
                    kind: .metadata,
                    result: result
                )
            )
        }
        XCTAssertEqual(
            FolderReconciliationExecutor.bounded(examined: 3, unreadable: 1, notResident: 2, failed: false),
            FolderReconciliationResult(examined: 3, notResident: 2, unreadable: 1)
        )
    }

    // MARK: - Coverage that did not complete

    /// A pass that reached nothing is runnable again inside one driver tick, and incomplete
    /// coverage is attention from the instant it is reported — not when a 15-minute or 24-hour
    /// interval happens to come round.
    func testCoverageThatDidNotCompleteStaysOverdueAndPromptlyRunnable() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try arm(lane)
        try record(lane, members: ["a.txt"])
        lane.codec.makeStale()

        let runs = await executor(lane).run(demands(lane, .metadata))

        XCTAssertEqual(runs.map(\.outcome), [.providerUnavailable])
        let coverage = lane.scheduler.coverage(subscriptionID: Self.id, kind: .metadata)
        XCTAssertTrue(coverage.isOverdue(at: lane.clock.now))
        XCTAssertFalse(coverage.isUpToDate(at: lane.clock.now))
        XCTAssertEqual(
            coverage.dueAt,
            Self.epoch.addingTimeInterval(FolderReconciliationScheduler.retryInterval)
        )
        XCTAssertLessThanOrEqual(
            FolderReconciliationScheduler.retryInterval,
            FolderReconciliationDriver.tickInterval
        )
        // The kind that reported is owed again on its own interval; the content kind is still
        // carrying the trigger this lane armed and was never handed over, so it is not consulted
        // here.
        lane.clock.advance(FolderReconciliationScheduler.retryInterval)
        XCTAssertEqual(
            lane.scheduler.due().filter { $0.kind == .metadata },
            [
                FolderReconciliationScheduler.Demand(
                    subscriptionID: Self.id,
                    kind: .metadata,
                    trigger: .due
                ),
            ]
        )
    }

    func testPartialCoverageIsAttentionAsSoonAsItIsReported() async throws {
        let lane = try lane(files: ["a.txt": "one", "b.txt": "two"])
        try arm(lane)
        try record(lane, members: ["a.txt", "b.txt"])

        let runs = await executor(lane) { url in
            url.lastPathComponent == "b.txt" ? .notResident : .resident
        }.run(demands(lane, .content))

        XCTAssertEqual(runs.map(\.outcome), [.partial])
        let coverage = lane.scheduler.coverage(subscriptionID: Self.id, kind: .content)
        XCTAssertEqual(coverage.uncovered, 1)
        // A member whose bytes are provider-only is still provider-only a minute later, so the
        // kind keeps its interval — but it is never presented as up to date in the meantime.
        XCTAssertTrue(coverage.isOverdue(at: lane.clock.now))
        XCTAssertFalse(coverage.isUpToDate(at: lane.clock.now))
        XCTAssertEqual(
            coverage.dueAt,
            Self.epoch.addingTimeInterval(FolderReconciliationKind.content.interval)
        )
    }

    // MARK: - Lifecycle handoff

    /// What the companion does: a lifecycle entry point arms the schedule and hands the demands it
    /// returns to the executor, off the main actor. Nothing in the executor path touches the main
    /// actor, so this is the whole route.
    func testDriverDemandsRunOnTheExecutorOffTheMainActor() async throws {
        let lane = try lane(files: ["a.txt": "one"])
        try record(lane, members: ["a.txt"])
        let driver = FolderReconciliationDriver(
            subscriptions: lane.store,
            scheduler: lane.scheduler
        )
        let executor = executor(lane)

        let startup = driver.startup()
        let runs = await Task.detached { await executor.run(startup) }.value

        XCTAssertEqual(Set(startup.map(\.kind)), Set(FolderReconciliationKind.allCases))
        XCTAssertEqual(Set(runs.map(\.outcome)), [.complete])
        XCTAssertEqual(
            lane.scheduler.coverage(subscriptionID: Self.id).map(\.lastOutcome),
            [.complete, .complete]
        )
        // The metadata half acknowledged the obligation the driver's trigger merged; the content
        // half never does.
        XCTAssertEqual(runs.filter(\.acknowledged).map(\.kind), [.metadata])
        XCTAssertNil(lane.ledger.obligation(subscriptionID: Self.id))

        // A later wake arms both kinds again and runs them the same way.
        lane.clock.advance(FolderReconciliationKind.content.interval * 2)
        let wake = driver.wake()
        let second = await Task.detached { await executor.run(wake) }.value
        XCTAssertEqual(Set(wake.map(\.trigger)), [.wake])
        XCTAssertEqual(Set(second.map(\.outcome)), [.complete])
    }

    // MARK: - The production residency probe

    /// Production never claims residency it cannot qualify, and never opens a file to find out.
    func testTheProductionResidencyProbeQualifiesBeforeItClaims() throws {
        let lane = try lane(files: ["a.txt": "one"])
        let present = lane.root.appendingPathComponent("a.txt")

        // An ordinary local file answers both questions, so it stays covered.
        XCTAssertEqual(FolderReconciliationExecutor.systemResidency(present), .resident)
        // A path whose attributes cannot be read qualifies nothing, and is never assumed local.
        XCTAssertEqual(
            FolderReconciliationExecutor.systemResidency(
                lane.root.appendingPathComponent("missing.txt")
            ),
            .unreadable
        )
        XCTAssertEqual(lane.reader.reads, [])
    }

    // MARK: - Lane

    private func lane(
        files: [String: String],
        recursive: Bool = true,
        activateRoot: Bool = true
    ) throws -> Lane {
        let directory = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("omnivia-wp11-executor-" + UUID().uuidString, isDirectory: true)
        let root = directory.appendingPathComponent("watched", isDirectory: true)
        let state = directory.appendingPathComponent("state", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock {
            for name in ["locked"] {
                try? FileManager.default.setAttributes(
                    [.posixPermissions: 0o700],
                    ofItemAtPath: root.appendingPathComponent(name).path
                )
            }
            try? FileManager.default.removeItem(at: directory)
        }
        for (path, contents) in files.sorted(by: { $0.key < $1.key }) {
            let file = root.appendingPathComponent(path)
            try FileManager.default.createDirectory(
                at: file.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try Data(contents.utf8).write(to: file)
        }
        let gate = PublicationGate()
        let codec = Codec()
        let accessStore = try NativeAccessStore(
            stateDirectory: state.appendingPathComponent("native-access", isDirectory: true),
            codec: codec,
            gate: gate
        )
        let store = try FolderSubscriptionStore(
            stateDirectory: state.appendingPathComponent("subscriptions", isDirectory: true),
            gate: gate
        )
        let descriptor = try accessStore.adoptRoot(
            rootRef: "root-" + Self.id,
            ownerRef: Self.id,
            selectedURL: root,
            binding: NativeRootAccessBinding(
                targetRef: "target-1",
                workspaceRef: "workspace-1",
                targetBindingRevision: "binding-1",
                callerContextBinding: Self.caller
            ),
            authority: .watchAndMembers
        )
        if activateRoot {
            try accessStore.activateRoot(
                rootRef: descriptor.rootRef,
                expectedGeneration: descriptor.generation
            )
        }
        try store.register(Self.subscription(recursive: recursive))
        let ledger = try FolderObservationLedger(
            stateDirectory: state.appendingPathComponent("observation", isDirectory: true),
            subscriptions: store,
            gate: gate
        )
        let status = try ledger.beginStream(
            identity: FolderObservationStreamIdentity(deviceID: Self.device, streamUUID: Self.uuidA),
            startEventID: 100,
            subscriptions: [
                FolderObservationApplicableSubscription(subscriptionID: Self.id, configGeneration: 1),
            ]
        )
        let clock = Clock(Self.epoch)
        let scheduler = try FolderReconciliationScheduler(
            stateDirectory: state.appendingPathComponent("schedule", isDirectory: true),
            subscriptions: store,
            observation: ledger,
            clock: { clock.now },
            jitter: { _, _ in 0 },
            gate: gate
        )
        return Lane(
            root: root,
            state: state,
            gate: gate,
            accessStore: accessStore,
            store: store,
            ledger: ledger,
            scheduler: scheduler,
            codec: codec,
            reader: CountingReader(),
            clock: clock,
            rootGeneration: descriptor.generation,
            observationEpoch: status.observationEpoch
        )
    }

    private func executor(
        _ lane: Lane,
        limits: FolderScopeLimits = .standard,
        residency: @escaping FolderContentResidencyProbe = { _ in .resident }
    ) -> FolderReconciliationExecutor {
        FolderReconciliationExecutor(
            accessStore: lane.accessStore,
            subscriptions: lane.store,
            observation: lane.ledger,
            scheduler: lane.scheduler,
            reader: lane.reader,
            gate: lane.gate,
            residency: residency,
            controlledStores: [lane.state],
            limits: limits
        )
    }

    /// Arms both kinds the way a lifecycle trigger does, so a demand the executor is handed is one
    /// the schedule really owes.
    private func arm(_ lane: Lane) throws {
        try lane.scheduler.request(subscriptionIDs: [Self.id], trigger: .startup)
    }

    private func demands(
        _ lane: Lane,
        _ kind: FolderReconciliationKind
    ) -> [FolderReconciliationScheduler.Demand] {
        lane.scheduler.due().filter { $0.kind == kind }
    }

    /// Records `paths` as present members with grants and digests that match what is on disk, so
    /// the only drift in a test is drift the test created.
    private func record(_ lane: Lane, members paths: [String], byteLength: UInt64? = nil) throws {
        let values = try paths.map { path in
            try member(lane, path, byteLength: byteLength)
        }
        try lane.store.update(id: Self.id) { $0.members = values }
    }

    /// `generationOffset` records a member against a grant generation it does not hold, which is
    /// the only way to express a frozen grant that no longer resolves: the generation is immutable
    /// on the member, and re-adopting the same reference replays the grant it already has.
    private func member(
        _ lane: Lane,
        _ path: String,
        digest: String? = "",
        byteLength: UInt64? = nil,
        generationOffset: UInt64 = 0
    ) throws -> FolderSubscriptionMember {
        let components = path.split(separator: "/").map(String.init)
        let reference = path.replacingOccurrences(of: "/", with: "-")
            .replacingOccurrences(of: ".", with: "-")
        let bytes = try Data(contentsOf: lane.root.appendingPathComponent(path))
        let descriptor = try lane.accessStore.adoptRootMember(
            accessRef: "access-" + reference,
            linkedDocumentID: "doc-" + reference,
            rootRef: "root-" + Self.id,
            memberRef: "member-" + reference,
            relativeComponents: components,
            expectedRootGeneration: lane.rootGeneration
        )
        return FolderSubscriptionMember(
            memberID: "member-" + reference,
            accessRef: "access-" + reference,
            linkedDocumentID: "doc-" + reference,
            requestedSourceIdentity: "source-" + reference,
            relativeComponents: components,
            displayName: path,
            contentKind: .text,
            membershipGeneration: descriptor.generation + generationOffset,
            baselineItemID: "item-" + reference,
            currentSuccessfulCaptureID: "capture-" + reference,
            lastObservedDigest: digest == "" ? Self.digest(bytes) : digest,
            lastObservedByteLength: byteLength ?? UInt64(bytes.count),
            lastObservedAt: "2026-10-14T00:00:00Z",
            lastQueuedItemID: nil,
            dirtyEpoch: 1,
            state: .present
        )
    }

    private static let caller = CallerContextBinding(
        clientContextRef: "client-1",
        principalRef: "principal-1",
        contextRevision: "context-1"
    )

    private static func digest(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private static func subscription(recursive: Bool) -> FolderSubscription {
        FolderSubscription(
            subscriptionID: id,
            rootRef: "root-" + id,
            rootDisplayName: "Watched",
            targetRef: "target-1",
            workspaceRef: "workspace-1",
            targetBindingRevision: "binding-1",
            callerContextBinding: caller,
            recursive: recursive,
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
