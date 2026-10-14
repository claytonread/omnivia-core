@testable import DocumentIntakeMacOS
import Foundation
import XCTest

/// WP-11 slice 1: the durable observation checkpoint. Each test drives the ledger through a real
/// `FolderSubscriptionStore` and a real state directory, and rebuilds it from disk where restart
/// matters.
final class FolderObservationLedgerTests: XCTestCase {
    private struct Lane {
        let directory: URL
        let store: FolderSubscriptionStore
        let gate: PublicationGate
    }

    private static let uuidA = "11111111-1111-4111-8111-111111111111"
    private static let uuidB = "22222222-2222-4222-8222-222222222222"
    private static let device: Int64 = 16_777_230

    private func lane(
        subscriptions: [(id: String, generation: UInt64, recursive: Bool, hidden: Bool)] = [
            ("sub-a", 1, true, false), ("sub-b", 1, true, false),
        ]
    ) throws -> Lane {
        let directory = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("omnivia-observation-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock {
            try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
            try? FileManager.default.removeItem(at: directory)
        }
        let gate = PublicationGate()
        let store = try FolderSubscriptionStore(
            stateDirectory: directory.appendingPathComponent("subscriptions"),
            gate: gate
        )
        for value in subscriptions {
            try store.register(Self.subscription(value.id, value.generation, value.recursive, value.hidden))
        }
        return Lane(directory: directory, store: store, gate: gate)
    }

    private static func subscription(
        _ id: String,
        _ generation: UInt64,
        _ recursive: Bool,
        _ hidden: Bool
    ) -> FolderSubscription {
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
            recursive: recursive,
            includeHidden: hidden,
            configGeneration: generation,
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

    private func open(_ lane: Lane) throws -> FolderObservationLedger {
        try FolderObservationLedger(
            stateDirectory: lane.directory.appendingPathComponent("observation"),
            subscriptions: lane.store,
            gate: lane.gate
        )
    }

    private var identity: FolderObservationStreamIdentity {
        FolderObservationStreamIdentity(deviceID: Self.device, streamUUID: Self.uuidA)
    }

    private func applicable(_ ids: String...) -> [FolderObservationApplicableSubscription] {
        ids.map { FolderObservationApplicableSubscription(subscriptionID: $0, configGeneration: 1) }
    }

    /// A stream whose first full reconciliations are already acknowledged, so tests start clean.
    private func settledLedger(_ lane: Lane, startEventID: UInt64 = 100) throws -> FolderObservationLedger {
        let ledger = try open(lane)
        let status = try ledger.beginStream(
            identity: identity,
            startEventID: startEventID,
            subscriptions: applicable("sub-a", "sub-b")
        )
        for id in ["sub-a", "sub-b"] {
            let revision = try XCTUnwrap(ledger.obligation(subscriptionID: id)).revision
            try ledger.acknowledge(
                subscriptionID: id,
                configGeneration: 1,
                observationEpoch: status.observationEpoch,
                completion: .full(throughRevision: revision)
            )
        }
        return ledger
    }

    private func event(_ id: UInt64, _ reason: FolderObservationEventReason) -> FolderObservationEvent {
        event(id, "sub-a", reason)
    }

    private func event(
        _ id: UInt64,
        _ subscription: String,
        _ reason: FolderObservationEventReason
    ) -> FolderObservationEvent {
        FolderObservationEvent(
            eventID: id,
            subscriptionID: subscription,
            configGeneration: 1,
            reason: reason
        )
    }

    private func member(_ components: String...) -> FolderObservationEventReason {
        .scope(FolderObservationScope(kind: .member, components: components))
    }

    private func subtree(_ components: String...) -> FolderObservationEventReason {
        .scope(FolderObservationScope(kind: .subtree, components: components))
    }

    private func batch(
        _ events: [FolderObservationEvent],
        epoch: UInt64 = 1,
        uuid: String? = FolderObservationLedgerTests.uuidA
    ) -> FolderObservationBatch {
        FolderObservationBatch(
            identity: FolderObservationStreamIdentity(deviceID: Self.device, streamUUID: uuid),
            observationEpoch: epoch,
            events: events
        )
    }

    private func stateFile(_ lane: Lane) -> URL {
        lane.directory.appendingPathComponent("observation/folder-observation-v1.json")
    }

    // MARK: - Restart

    func testIngestedObligationsAndCursorSurviveACleanRestart() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        XCTAssertEqual(
            try ledger.ingest(batch([event(101, member("a.txt")), event(105, "sub-b", subtree("dir"))])),
            105
        )
        try ledger.markCleanShutdown()

        let reopened = try open(lane)
        XCTAssertEqual(reopened.stream(deviceID: Self.device)?.lastCoveredEventID, 105)
        XCTAssertEqual(reopened.stream(deviceID: Self.device)?.catchUp, false)
        XCTAssertEqual(reopened.obligation(subscriptionID: "sub-a")?.scopes.map(\.scope.components), [["a.txt"]])
        XCTAssertEqual(reopened.obligation(subscriptionID: "sub-b")?.scopes.map(\.scope.kind), [.subtree])
        XCTAssertNil(reopened.obligation(subscriptionID: "sub-a")?.fullRevision)
    }

    func testReopeningAnUncleanRunOwesFullReconciliationForEveryAffectedSubscription() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        try ledger.ingest(batch([event(120, member("a.txt"))]))
        // No markCleanShutdown: the process died.

        let reopened = try open(lane)
        let status = try XCTUnwrap(reopened.stream(deviceID: Self.device))
        XCTAssertTrue(status.catchUp)
        XCTAssertEqual(status.lastCoveredEventID, 120)
        for id in ["sub-a", "sub-b"] {
            let obligation = try XCTUnwrap(reopened.obligation(subscriptionID: id))
            XCTAssertNotNil(obligation.fullRevision)
            XCTAssertEqual(obligation.reasons, [.uncleanRun])
            XCTAssertTrue(obligation.scopes.isEmpty)
        }
        // Catch-up ends only when the full reconciliations are acknowledged.
        for id in ["sub-a", "sub-b"] {
            XCTAssertTrue(try XCTUnwrap(reopened.stream(deviceID: Self.device)).catchUp)
            try reopened.acknowledge(
                subscriptionID: id,
                configGeneration: 1,
                observationEpoch: status.observationEpoch,
                completion: .full(throughRevision: try XCTUnwrap(reopened.obligation(subscriptionID: id)).revision)
            )
        }
        XCTAssertEqual(reopened.stream(deviceID: Self.device)?.catchUp, false)
    }

    func testAFreshLedgerStartsUncleanUntilItIsCheckpointed() throws {
        let lane = try lane()
        _ = try open(lane)
        let document = try JSONSerialization.jsonObject(with: Data(contentsOf: stateFile(lane))) as? [String: Any]
        XCTAssertEqual(document?["cleanShutdown"] as? Bool, false)
    }

    func testACheckpointedLedgerRefusesFurtherRecording() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        try ledger.markCleanShutdown()
        XCTAssertThrowsError(try ledger.ingest(batch([event(101, member("a.txt"))]))) {
            XCTAssertEqual($0 as? FolderObservationLedgerError, .closed)
        }
    }

    // MARK: - Atomicity

    func testAFailedWriteAdvancesNeitherTheCursorNorTheObligations() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        let observation = lane.directory.appendingPathComponent("observation")
        // The atomic temp write cannot be created in a read-only directory.
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: observation.path)
        XCTAssertThrowsError(try ledger.ingest(batch([event(130, member("a.txt"))]))) {
            XCTAssertEqual($0 as? FolderObservationLedgerError, .stateUnavailable)
        }
        XCTAssertEqual(ledger.stream(deviceID: Self.device)?.lastCoveredEventID, 100)
        XCTAssertNil(ledger.obligation(subscriptionID: "sub-a"))

        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: observation.path)
        XCTAssertEqual(try ledger.ingest(batch([event(130, member("a.txt"))])), 130)
        XCTAssertEqual(ledger.obligation(subscriptionID: "sub-a")?.scopes.count, 1)
    }

    func testABatchWithOneBadEventChangesNothing() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        XCTAssertThrowsError(
            try ledger.ingest(batch([event(101, member("a.txt")), event(102, member("..", "x"))]))
        ) {
            XCTAssertEqual($0 as? FolderObservationLedgerError, .malformedScope)
        }
        XCTAssertEqual(ledger.stream(deviceID: Self.device)?.lastCoveredEventID, 100)
        XCTAssertNil(ledger.obligation(subscriptionID: "sub-a"))
    }

    // MARK: - Rejections

    func testEventIDsThatDecreaseOrAreReservedAreRejected() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        let regressed: [[FolderObservationEvent]] = [
            [event(99, member("a.txt"))],
            [event(110, member("a.txt")), event(109, member("b.txt"))],
            [event(UInt64.max, member("a.txt"))],
        ]
        for events in regressed {
            XCTAssertThrowsError(try ledger.ingest(batch(events))) {
                XCTAssertEqual($0 as? FolderObservationLedgerError, .eventIDRegressed)
            }
        }
        // Replaying the covered ID itself is harmless.
        XCTAssertEqual(try ledger.ingest(batch([event(100, member("a.txt"))])), 100)
    }

    func testUnrelatedStreamsEpochsAndSubscriptionsAreRejected() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        func expect(_ error: FolderObservationLedgerError, _ value: FolderObservationBatch) {
            XCTAssertThrowsError(try ledger.ingest(value)) { XCTAssertEqual($0 as? FolderObservationLedgerError, error) }
        }
        expect(.streamIdentityChanged, batch([event(101, member("a"))], uuid: Self.uuidB))
        expect(.streamIdentityChanged, batch([event(101, member("a"))], uuid: nil))
        expect(.staleEpoch, batch([event(101, member("a"))], epoch: 2))
        expect(.unknownSubscription, batch([event(101, "sub-z", member("a"))]))
        expect(.staleConfigGeneration, batch([
            FolderObservationEvent(eventID: 101, subscriptionID: "sub-a", configGeneration: 2, reason: member("a")),
        ]))
        expect(.emptyBatch, batch([]))
        expect(.invalidReason, batch([event(101, .full(.uncleanRun))]))
        XCTAssertThrowsError(try ledger.ingest(
            FolderObservationBatch(
                identity: FolderObservationStreamIdentity(deviceID: 7, streamUUID: Self.uuidA),
                observationEpoch: 1,
                events: [event(101, member("a"))]
            )
        )) { XCTAssertEqual($0 as? FolderObservationLedgerError, .unknownStream) }
        XCTAssertEqual(ledger.stream(deviceID: Self.device)?.lastCoveredEventID, 100)
    }

    func testAConfigGenerationThatMovedOnInTheStoreIsStale() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        _ = try lane.store.register(Self.subscription("sub-c", 1, true, false))
        XCTAssertThrowsError(
            try ledger.beginStream(
                identity: identity,
                startEventID: 0,
                subscriptions: [FolderObservationApplicableSubscription(subscriptionID: "sub-c", configGeneration: 5)]
            )
        ) { XCTAssertEqual($0 as? FolderObservationLedgerError, .staleConfigGeneration) }
        XCTAssertThrowsError(
            try ledger.beginStream(
                identity: FolderObservationStreamIdentity(deviceID: 9, streamUUID: nil),
                startEventID: 0,
                subscriptions: applicable("sub-a")
            )
        ) { XCTAssertEqual($0 as? FolderObservationLedgerError, .duplicateSubscription) }
    }

    func testMalformedAndOutOfScopeComponentsAreRejected() throws {
        let lane = try lane(subscriptions: [
            ("sub-a", 1, true, false), ("sub-b", 1, false, false),
        ])
        let ledger = try settledLedger(lane)
        let tooDeep = Array(repeating: "d", count: FolderObservationLedger.maximumComponentDepth + 1)
        let malformed: [FolderObservationScope] = [
            FolderObservationScope(kind: .member, components: []),
            FolderObservationScope(kind: .member, components: [""]),
            FolderObservationScope(kind: .member, components: ["."]),
            FolderObservationScope(kind: .member, components: ["a", ".."]),
            FolderObservationScope(kind: .member, components: ["a/b"]),
            FolderObservationScope(kind: .member, components: ["a\u{0}b"]),
            FolderObservationScope(kind: .member, components: ["a\nb"]),
            FolderObservationScope(kind: .member, components: [String(repeating: "x", count: 256)]),
            FolderObservationScope(kind: .subtree, components: tooDeep),
        ]
        for scope in malformed {
            XCTAssertThrowsError(try ledger.ingest(batch([event(101, .scope(scope))])), "\(scope)") {
                XCTAssertEqual($0 as? FolderObservationLedgerError, .malformedScope)
            }
        }
        // Hidden names the subscription did not approve, and depth a non-recursive one never watches.
        for reason in [member(".hidden"), member("dir", ".secret", "x"), subtree(".git")] {
            XCTAssertThrowsError(try ledger.ingest(batch([event(101, reason)]))) {
                XCTAssertEqual($0 as? FolderObservationLedgerError, .outOfScope)
            }
        }
        for reason in [member("dir", "a.txt"), subtree("dir")] {
            XCTAssertThrowsError(try ledger.ingest(batch([event(101, "sub-b", reason)]))) {
                XCTAssertEqual($0 as? FolderObservationLedgerError, .outOfScope)
            }
        }
        XCTAssertEqual(try ledger.ingest(batch([event(101, "sub-b", member("a.txt"))])), 101)
    }

    func testCorruptDuplicateOversizedAndFutureStateFailsClosed() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        try ledger.ingest(batch([event(101, member("a.txt"))]))
        try ledger.markCleanShutdown()
        let file = stateFile(lane)
        let original = try Data(contentsOf: file)
        let text = try XCTUnwrap(String(data: original, encoding: .utf8))

        func expectOpenFails(_ replacement: Data, _ error: FolderObservationLedgerError, _ label: String) throws {
            try replacement.write(to: file)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
            XCTAssertThrowsError(try open(lane), label) { XCTAssertEqual($0 as? FolderObservationLedgerError, error, label) }
        }
        try expectOpenFails(
            Data(text.replacingOccurrences(of: "folder-observation.v1", with: "folder-observation.v2").utf8),
            .unsupportedSchema, "future schema"
        )
        try expectOpenFails(Data("{\"schema\":\"omnivia.folder-observation.v2\",\"other\":1}".utf8), .unsupportedSchema, "future shape")
        try expectOpenFails(Data("not json".utf8), .invalidState, "garbage")
        try expectOpenFails(
            Data(text.replacingOccurrences(of: "\"subscriptionID\":\"sub-b\"", with: "\"subscriptionID\":\"sub-a\"").utf8),
            .invalidState, "duplicate subscription"
        )
        try expectOpenFails(
            Data(text.replacingOccurrences(of: "\"observationEpoch\":1", with: "\"observationEpoch\":0").utf8),
            .invalidState, "zero epoch"
        )
        try expectOpenFails(
            Data(text.replacingOccurrences(of: "\"catchUp\":false", with: "\"catchUp\":true").utf8),
            .invalidState, "inconsistent catch-up"
        )
        try expectOpenFails(
            Data(text.replacingOccurrences(of: "\"revision\":3,\"streamUUID\"", with: "\"revision\":1,\"streamUUID\"").utf8),
            .invalidState, "revision beyond the stream counter"
        )
        try expectOpenFails(
            Data(repeating: 0x20, count: FolderObservationLedger.maximumStateBytes + 1),
            .invalidState, "oversized"
        )
        try original.write(to: file)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: file.path)
        XCTAssertThrowsError(try open(lane), "group-readable") {
            XCTAssertEqual($0 as? FolderObservationLedgerError, .invalidState)
        }
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        XCTAssertNoThrow(try open(lane))
    }

    // MARK: - Epochs

    func testAChangedStreamIdentityOpensANewEpochWithFullObligationsAndNoComparableCursor() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        try ledger.ingest(batch([event(5000, member("a.txt"))]))
        let before = try XCTUnwrap(ledger.obligation(subscriptionID: "sub-a")).revision

        let changed = FolderObservationStreamIdentity(deviceID: Self.device, streamUUID: Self.uuidB)
        let status = try ledger.beginStream(
            identity: changed,
            startEventID: 7,
            subscriptions: applicable("sub-a", "sub-b")
        )
        XCTAssertEqual(status.observationEpoch, 2)
        XCTAssertEqual(status.lastCoveredEventID, 7, "the old, unrelated ID must not be compared with")
        for id in ["sub-a", "sub-b"] {
            let obligation = try XCTUnwrap(ledger.obligation(subscriptionID: id))
            XCTAssertEqual(obligation.reasons, [.streamChanged])
            XCTAssertTrue(obligation.scopes.isEmpty)
            XCTAssertGreaterThan(obligation.revision, before)
        }
        // Old-epoch traffic and old-epoch acknowledgements are refused.
        XCTAssertThrowsError(try ledger.ingest(batch([event(5001, member("a"))], epoch: 1, uuid: Self.uuidB))) {
            XCTAssertEqual($0 as? FolderObservationLedgerError, .staleEpoch)
        }
        XCTAssertThrowsError(try ledger.acknowledge(
            subscriptionID: "sub-a",
            configGeneration: 1,
            observationEpoch: 1,
            completion: .full(throughRevision: UInt64.max - 1)
        )) { XCTAssertEqual($0 as? FolderObservationLedgerError, .staleEpoch) }
        XCTAssertNotNil(ledger.obligation(subscriptionID: "sub-a")?.fullRevision)
        // The new epoch ingests from its own base.
        XCTAssertEqual(try ledger.ingest(batch([event(8, member("n.txt"))], epoch: 2, uuid: Self.uuidB)), 8)
    }

    func testWrappedEventIDsCanOnlyContinueThroughANewEpoch() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane, startEventID: UInt64.max - 10)
        XCTAssertThrowsError(try ledger.ingest(batch([event(3, member("a.txt"))]))) {
            XCTAssertEqual($0 as? FolderObservationLedgerError, .eventIDRegressed)
        }
        let status = try ledger.beginStream(
            identity: identity,
            startEventID: 3,
            subscriptions: applicable("sub-a", "sub-b"),
            startNewEpoch: true
        )
        XCTAssertEqual(status.observationEpoch, 2)
        XCTAssertEqual(ledger.obligation(subscriptionID: "sub-a")?.reasons, [.streamChanged])
    }

    func testNewSubscriptionsAndConfigChangesOweFullReconciliationOnAKnownStream() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        try ledger.ingest(batch([event(101, member("a.txt"))]))
        _ = try lane.store.update(id: "sub-a") { _ in }
        try lane.store.register(Self.subscription("sub-c", 1, true, false))

        let status = try ledger.beginStream(
            identity: identity,
            startEventID: 0,
            subscriptions: applicable("sub-a", "sub-b", "sub-c")
        )
        XCTAssertEqual(status.observationEpoch, 1)
        XCTAssertEqual(status.lastCoveredEventID, 101, "a known stream keeps its cursor")
        XCTAssertEqual(ledger.obligation(subscriptionID: "sub-c")?.reasons, [.subscriptionAdded])
        XCTAssertEqual(ledger.obligation(subscriptionID: "sub-a")?.scopes.count, 1)
        XCTAssertNil(ledger.obligation(subscriptionID: "sub-b"))

        // Dropping a subscription drops its obligations.
        _ = try ledger.beginStream(identity: identity, startEventID: 0, subscriptions: applicable("sub-a"))
        XCTAssertNil(ledger.obligation(subscriptionID: "sub-c"))
    }

    // MARK: - Coalescing and bounds

    func testScopesCoalesceDeterministically() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        try ledger.ingest(batch([
            event(101, member("b", "z.txt")),
            event(102, member("b", "a.txt")),
            event(103, member("a.txt")),
            event(104, member("b", "a.txt")),
            event(105, subtree("c", "d")),
            event(106, member("c", "d", "x.txt")),
        ]))
        func components(_ ledger: FolderObservationLedger) -> [[String]] {
            ledger.obligation(subscriptionID: "sub-a")?.scopes.map(\.scope.components) ?? []
        }
        XCTAssertEqual(components(ledger), [["a.txt"], ["b", "a.txt"], ["b", "z.txt"], ["c", "d"]])

        // A subtree subsumes everything beneath it, including a member at its own path.
        try ledger.ingest(batch([event(107, subtree("b")), event(108, member("c", "d"))]))
        let scopes = try XCTUnwrap(ledger.obligation(subscriptionID: "sub-a")).scopes
        XCTAssertEqual(scopes.map(\.scope.components), [["a.txt"], ["b"], ["c", "d"]])
        XCTAssertEqual(scopes.map(\.scope.kind), [.member, .subtree, .subtree])

        // The same events in another order give the same ledger.
        let other = try self.lane()
        let second = try settledLedger(other)
        try second.ingest(batch([
            event(101, subtree("b")),
            event(102, member("c", "d", "x.txt")),
            event(103, subtree("c", "d")),
            event(104, member("a.txt")),
        ]))
        XCTAssertEqual(components(second), components(ledger))
    }

    func testFullReconciliationSubsumesScopesAndEveryReasonIsKept() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        try ledger.ingest(batch([
            event(101, member("a.txt")),
            event(102, .full(.mustScanSubDirs)),
            event(103, member("b.txt")),
            event(104, .full(.eventsDropped)),
            event(105, .full(.mustScanSubDirs)),
        ]))
        let obligation = try XCTUnwrap(ledger.obligation(subscriptionID: "sub-a"))
        XCTAssertTrue(obligation.scopes.isEmpty)
        XCTAssertEqual(obligation.reasons, [.eventsDropped, .mustScanSubDirs])
        XCTAssertNil(ledger.obligation(subscriptionID: "sub-b"), "other subscriptions are untouched")
    }

    func testTooManyScopesCollapseToOneFullReconciliation() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        let limit = FolderObservationLedger.maximumScopesPerSubscription
        let events = (0...limit).map { event(101 + UInt64($0), member("f\($0).txt")) }
        try ledger.ingest(batch(events))
        let obligation = try XCTUnwrap(ledger.obligation(subscriptionID: "sub-a"))
        XCTAssertNotNil(obligation.fullRevision)
        XCTAssertEqual(obligation.reasons, [.overflow])
        XCTAssertTrue(obligation.scopes.isEmpty)
    }

    func testBatchAndStateSizesAreBounded() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        let oversized = (0...FolderObservationLedger.maximumEventsPerBatch).map {
            event(101 + UInt64($0), member("a.txt"))
        }
        XCTAssertThrowsError(try ledger.ingest(batch(oversized))) {
            XCTAssertEqual($0 as? FolderObservationLedgerError, .tooLarge)
        }
        XCTAssertEqual(ledger.stream(deviceID: Self.device)?.lastCoveredEventID, 100)
        let size = try Data(contentsOf: stateFile(lane)).count
        XCTAssertLessThan(size, FolderObservationLedger.maximumStateBytes)
    }

    // MARK: - Acknowledgement

    func testAcknowledgementClearsOnlyWhatWasCompletedAndKeepsNewerObligations() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        try ledger.ingest(batch([event(101, member("a.txt")), event(102, member("b.txt"))]))
        let snapshot = try XCTUnwrap(ledger.obligation(subscriptionID: "sub-a"))
        // A newer event arrives while the reconciliation is running.
        try ledger.ingest(batch([event(103, member("a.txt")), event(104, member("c.txt"))]))

        try ledger.acknowledge(
            subscriptionID: "sub-a",
            configGeneration: 1,
            observationEpoch: 1,
            completion: .scopes(
                [FolderObservationScope(kind: .member, components: ["a.txt"]),
                 FolderObservationScope(kind: .member, components: ["b.txt"])],
                throughRevision: snapshot.revision
            )
        )
        // a.txt was re-dirtied after the snapshot, so it stays; b.txt was completed; c.txt is newer.
        XCTAssertEqual(
            ledger.obligation(subscriptionID: "sub-a")?.scopes.map(\.scope.components),
            [["a.txt"], ["c.txt"]]
        )
        // A scope that was never named is never cleared.
        try ledger.acknowledge(
            subscriptionID: "sub-a", configGeneration: 1, observationEpoch: 1,
            completion: .scopes([FolderObservationScope(kind: .member, components: ["z.txt"])], throughRevision: 1_000)
        )
        XCTAssertEqual(ledger.obligation(subscriptionID: "sub-a")?.scopes.count, 2)
    }

    func testAFullAcknowledgementDoesNotEraseEventsThatArrivedDuringTheScan() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        try ledger.ingest(batch([event(101, .full(.eventsDropped))]))
        let snapshot = try XCTUnwrap(ledger.obligation(subscriptionID: "sub-a"))
        try ledger.ingest(batch([event(102, member("late.txt"))]))

        try ledger.acknowledge(
            subscriptionID: "sub-a", configGeneration: 1, observationEpoch: 1,
            completion: .full(throughRevision: snapshot.revision)
        )
        let remaining = try XCTUnwrap(ledger.obligation(subscriptionID: "sub-a"))
        XCTAssertNotNil(remaining.fullRevision, "the scan began before the late event")

        try ledger.acknowledge(
            subscriptionID: "sub-a", configGeneration: 1, observationEpoch: 1,
            completion: .full(throughRevision: remaining.revision)
        )
        XCTAssertNil(ledger.obligation(subscriptionID: "sub-a"))
    }

    func testAcknowledgementIsScopedToTheSameSubscriptionConfigAndEpoch() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        try ledger.ingest(batch([event(101, member("a.txt")), event(102, "sub-b", member("b.txt"))]))
        func expect(_ error: FolderObservationLedgerError, _ id: String, _ generation: UInt64, _ epoch: UInt64) {
            XCTAssertThrowsError(try ledger.acknowledge(
                subscriptionID: id, configGeneration: generation, observationEpoch: epoch,
                completion: .full(throughRevision: 1_000)
            )) { XCTAssertEqual($0 as? FolderObservationLedgerError, error) }
        }
        expect(.unknownSubscription, "sub-z", 1, 1)
        expect(.staleConfigGeneration, "sub-a", 2, 1)
        expect(.staleEpoch, "sub-a", 1, 2)
        XCTAssertEqual(ledger.obligation(subscriptionID: "sub-a")?.scopes.count, 1)
        XCTAssertEqual(ledger.obligation(subscriptionID: "sub-b")?.scopes.count, 1)

        try ledger.acknowledge(
            subscriptionID: "sub-a", configGeneration: 1, observationEpoch: 1,
            completion: .full(throughRevision: 1_000)
        )
        XCTAssertNil(ledger.obligation(subscriptionID: "sub-a"))
        XCTAssertEqual(ledger.obligation(subscriptionID: "sub-b")?.scopes.count, 1)
    }

    func testARevisionIsNeverReusedAfterAnObligationIsCleared() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        try ledger.ingest(batch([event(101, member("a.txt"))]))
        let stale = try XCTUnwrap(ledger.obligation(subscriptionID: "sub-a")).revision
        try ledger.acknowledge(
            subscriptionID: "sub-a", configGeneration: 1, observationEpoch: 1,
            completion: .full(throughRevision: stale)
        )
        try ledger.ingest(batch([event(102, member("a.txt"))]))
        // A duplicate delivery of the first acknowledgement must not erase the new obligation.
        try ledger.acknowledge(
            subscriptionID: "sub-a", configGeneration: 1, observationEpoch: 1,
            completion: .full(throughRevision: stale)
        )
        XCTAssertEqual(ledger.obligation(subscriptionID: "sub-a")?.scopes.count, 1)
    }

    // MARK: - Privacy and wiring

    func testThePersistedStateHoldsOnlyOpaqueIDsAndRelativeComponentsWithOwnerOnlyModes() throws {
        let lane = try lane()
        let ledger = try settledLedger(lane)
        try ledger.ingest(batch([event(101, member("Reports", "q3.docx")), event(102, subtree("Taxes"))]))
        try ledger.markCleanShutdown()

        let text = try String(contentsOf: stateFile(lane), encoding: .utf8)
        XCTAssertTrue(text.contains("q3.docx"))
        XCTAssertFalse(text.contains("/private"), "no absolute path may reach the ledger")
        XCTAssertFalse(text.contains(lane.directory.lastPathComponent))
        XCTAssertFalse(text.contains("\\/"))
        let file = try FileManager.default.attributesOfItem(atPath: stateFile(lane).path)
        XCTAssertEqual((file[.posixPermissions] as? NSNumber)?.intValue, 0o600)
        let directory = try FileManager.default.attributesOfItem(
            atPath: stateFile(lane).deletingLastPathComponent().path
        )
        XCTAssertEqual((directory[.posixPermissions] as? NSNumber)?.intValue, 0o700)
    }

    func testTheEnvironmentOwnsALedgerOnTheSharedGate() throws {
        let root = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("omnivia-observation-env-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let environment = try DocumentIntakeEnvironment(stateRoot: root)
        XCTAssertTrue(environment.folderObservation.streams().isEmpty)
        let file = root.appendingPathComponent("folder-observation/folder-observation-v1.json")
        XCTAssertTrue(FileManager.default.fileExists(atPath: file.path))
    }
}
