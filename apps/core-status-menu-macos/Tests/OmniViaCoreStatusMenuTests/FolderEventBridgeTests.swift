@testable import DocumentIntakeMacOS
import CoreServices
import Foundation
import XCTest

/// WP-11 slice 2: native event batches reach the durable ledger before anything is signalled.
/// Everything but the last test drives the bridge through a recording platform; no FSEvents
/// stream runs.
final class FolderEventBridgeTests: XCTestCase {
    private static let uuidA = "11111111-1111-4111-8111-111111111111"
    private static let uuidB = "22222222-2222-4222-8222-222222222222"
    private static let base = "/private/tmp/wp11-bridge"
    private static let file = UInt32(kFSEventStreamEventFlagItemIsFile)
    private static let directory = UInt32(kFSEventStreamEventFlagItemIsDir)

    private final class Handle: FolderEventStreamHandle, @unchecked Sendable {
        let stops = Counter()
        func stop() { stops.increment() }
    }

    private final class Counter: @unchecked Sendable {
        private let lock = NSLock()
        private var value = 0
        func increment() { lock.withLock { value += 1 } }
        var count: Int { lock.withLock { value } }
    }

    /// Roots under `…/dev2/` live on device 2; everything else on device 1.
    private final class Fake: @unchecked Sendable {
        struct Start {
            let paths: [String]
            let since: UInt64
            let deliver: @Sendable ([FolderNativeEvent]) -> Void
            let handle: Handle
        }

        private let lock = NSLock()
        private var _starts: [Start] = []
        var uuids: [Int64: String?] = [1: FolderEventBridgeTests.uuidA, 2: FolderEventBridgeTests.uuidB]
        var current: UInt64 = 100
        var failStart = false

        var starts: [Start] { lock.withLock { _starts } }

        var platform: FolderEventPlatform {
            FolderEventPlatform(
                locate: { url in
                    (URL(fileURLWithPath: url.path).standardizedFileURL.pathComponents, url.path.contains("/dev2/") ? 2 : 1)
                },
                streamUUID: { [unowned self] device in uuids[device] ?? nil },
                currentEventID: { [unowned self] in current },
                startStream: { [unowned self] paths, since, deliver in
                    guard !failStart else { return nil }
                    let handle = Handle()
                    lock.withLock { _starts.append(Start(paths: paths, since: since, deliver: deliver, handle: handle)) }
                    return handle
                }
            )
        }
    }

    private final class Signals: @unchecked Sendable {
        struct Seen {
            let ids: Set<String>
            let full: Bool
            /// What the ledger already held when the handler ran.
            let persistedScopes: [String: [[String]]]
        }

        private let lock = NSLock()
        private var seen: [Seen] = []
        var all: [Seen] { lock.withLock { seen } }
        func record(_ value: Seen) { lock.withLock { seen.append(value) } }
    }

    private struct Lane {
        let directory: URL
        let store: FolderSubscriptionStore
        let ledger: FolderObservationLedger
        let signals = Signals()
        let fake = Fake()
        var stateFile: URL { directory.appendingPathComponent("observation/folder-observation-v1.json") }
    }

    private func lane() throws -> Lane {
        let directory = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("omnivia-bridge-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o700],
                ofItemAtPath: directory.appendingPathComponent("observation").path
            )
            try? FileManager.default.removeItem(at: directory)
        }
        let gate = PublicationGate()
        let store = try FolderSubscriptionStore(
            stateDirectory: directory.appendingPathComponent("subscriptions"), gate: gate
        )
        try store.register(Self.subscription("sub-a", recursive: true))
        try store.register(Self.subscription("sub-b", recursive: true))
        try store.register(Self.subscription("sub-c", recursive: false))
        let ledger = try FolderObservationLedger(
            stateDirectory: directory.appendingPathComponent("observation"), subscriptions: store, gate: gate
        )
        return Lane(directory: directory, store: store, ledger: ledger)
    }

    private static func subscription(_ id: String, recursive: Bool) -> FolderSubscription {
        FolderSubscription(
            subscriptionID: id, rootRef: "root-\(id)", rootDisplayName: "Root", targetRef: "target-1",
            workspaceRef: "workspace-1", targetBindingRevision: "binding-1",
            callerContextBinding: CallerContextBinding(
                clientContextRef: "client-1", principalRef: "principal-1", contextRevision: "context-1"
            ),
            recursive: recursive, includeHidden: false, configGeneration: 1, consentGeneration: 1,
            consentRecordedAt: "2026-10-14T00:00:00Z", observationEpoch: "epoch-1", state: .active,
            needsFullReconciliation: false, lastCompletedMetadataScanAt: nil, pendingReviewSignature: nil,
            approvedReviewSignature: nil, reviewChangeCount: nil, reviewExamples: nil, members: []
        )
    }

    private static func root(_ id: String) -> FolderObservationRoot {
        let path = id == "sub-c" ? "\(base)/dev2/c" : "\(base)/\(id.dropFirst(4))"
        return FolderObservationRoot(subscriptionID: id, url: URL(fileURLWithPath: path, isDirectory: true))
    }

    private func bridge(
        _ lane: Lane,
        _ ids: [String] = ["sub-a", "sub-b"],
        onAttention: @escaping @Sendable (FolderObservationAttention) -> Void = { _ in }
    ) throws -> FolderEventBridge {
        let ledger = lane.ledger
        let signals = lane.signals
        return try XCTUnwrap(
            FolderEventBridge(
                roots: ids.map(Self.root), ledger: ledger, subscriptions: lane.store,
                platform: lane.fake.platform, onAttention: onAttention
            ) { signal in
                var scopes: [String: [[String]]] = [:]
                for id in signal.subscriptionIDs {
                    scopes[id] = ledger.obligation(subscriptionID: id)?.scopes.map(\.scope.components) ?? []
                }
                signals.record(.init(
                    ids: signal.subscriptionIDs, full: signal.requiresFullReconciliation, persistedScopes: scopes
                ))
            }
        )
    }

    private func event(_ id: UInt64, _ path: String, _ flags: UInt32 = FolderEventBridgeTests.file) -> FolderNativeEvent {
        FolderNativeEvent(eventID: id, path: path, flags: flags)
    }

    /// Settles the full reconciliations a fresh epoch owes, so a test can see only what a batch adds.
    private func settle(_ lane: Lane, _ ids: [String] = ["sub-a", "sub-b"]) throws {
        let epoch = try XCTUnwrap(lane.ledger.streams().first).observationEpoch
        for id in ids {
            try lane.ledger.acknowledge(
                subscriptionID: id, configGeneration: 1, observationEpoch: epoch,
                completion: .full(throughRevision: UInt64.max - 1)
            )
        }
    }

    // MARK: - Translation

    func testTranslationKeepsOnlyRootRelativeComponentsInsideRegisteredRoots() {
        let bindings = [
            FolderEventBinding(subscriptionID: "a", configGeneration: 1, root: ["/", "r", "a"], recursive: true, includeHidden: false),
            FolderEventBinding(subscriptionID: "docs", configGeneration: 1, root: ["/", "r", "a", "docs"], recursive: true, includeHidden: false),
            FolderEventBinding(subscriptionID: "c", configGeneration: 1, root: ["/", "r", "c"], recursive: false, includeHidden: false),
        ]
        let native = [
            event(1, "/r/a/docs/note.txt"),
            event(2, "/r/a/docs/sub", Self.directory),
            event(3, "/r/a/.git/config"),
            event(4, "/r/a-evil/secret.txt"),
            event(5, "/elsewhere/secret.txt"),
            event(6, "/r/c/top.txt"),
            event(7, "/r/c/dir/deep.txt"),
            event(8, "/r/a/\u{1}bad/x.txt"),
        ]
        let events = FolderEventTranslator.translate(native, bindings: bindings).events
        func scopes(_ id: String) -> [FolderObservationEventReason] {
            events.filter { $0.subscriptionID == id }.map(\.reason)
        }
        XCTAssertEqual(scopes("a"), [
            .scope(.init(kind: .member, components: ["docs", "note.txt"])),
            .scope(.init(kind: .subtree, components: ["docs", "sub"])),
            .full(.mustScanSubDirs),
        ])
        XCTAssertEqual(scopes("docs"), [
            .scope(.init(kind: .member, components: ["note.txt"])),
            .scope(.init(kind: .subtree, components: ["sub"])),
        ])
        XCTAssertEqual(scopes("c"), [.scope(.init(kind: .member, components: ["top.txt"]))])
        XCTAssertFalse("\(events)".contains("secret"))
        XCTAssertFalse("\(events)".contains("/r/"))
    }

    // MARK: - Persistence and privacy

    func testPersistsRootRelativeScopesBeforeTheHandlerAndKeepsPathsOut() throws {
        let lane = try lane()
        let bridge = try bridge(lane)
        try settle(lane)
        let startupSignals = lane.signals.all.count
        lane.fake.starts[0].deliver([
            event(150, "\(Self.base)/a/docs/report.txt"),
            event(151, "\(Self.base)/zzz/outside-secret-name.txt"),
        ])
        withExtendedLifetime(bridge) {}
        let signals = Array(lane.signals.all.dropFirst(startupSignals))
        XCTAssertEqual(signals.count, 1)
        XCTAssertEqual(signals[0].ids, ["sub-a"])
        XCTAssertFalse(signals[0].full)
        // The handler found the obligation already on disk-backed state.
        XCTAssertEqual(signals[0].persistedScopes["sub-a"], [["docs", "report.txt"]])
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.lastCoveredEventID, 150)
        let state = try String(contentsOf: lane.stateFile, encoding: .utf8)
        XCTAssertFalse(state.contains(Self.base))
        XCTAssertFalse(state.contains("outside-secret-name"))
        XCTAssertFalse(state.contains("/private/tmp"))
        XCTAssertTrue(state.contains("report.txt"))
    }

    func testBatchWithNothingInScopeNeitherWritesNorSignals() throws {
        let lane = try lane()
        let bridge = try bridge(lane)
        let before = lane.signals.all.count
        let covered = lane.ledger.stream(deviceID: 1)?.lastCoveredEventID
        lane.fake.starts[0].deliver([event(160, "\(Self.base)/zzz/x.txt"), event(161, "\(Self.base)/a/.hidden/x.txt")])
        withExtendedLifetime(bridge) {}
        XCTAssertEqual(lane.signals.all.count, before)
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.lastCoveredEventID, covered)
    }

    func testRootsOnOneDeviceShareAStreamAndEventsSplitByRoot() throws {
        let lane = try lane()
        let bridge = try bridge(lane)
        try settle(lane)
        XCTAssertEqual(lane.fake.starts.count, 1)
        XCTAssertEqual(lane.fake.starts[0].paths, ["\(Self.base)/a", "\(Self.base)/b"])
        lane.fake.starts[0].deliver([event(150, "\(Self.base)/b/x.txt")])
        withExtendedLifetime(bridge) {}
        XCTAssertNil(lane.ledger.obligation(subscriptionID: "sub-a"))
        XCTAssertEqual(lane.ledger.obligation(subscriptionID: "sub-b")?.scopes.map(\.scope.components), [["x.txt"]])
    }

    func testDevicesGetSeparateStreamsAndUUIDs() throws {
        let lane = try lane()
        let bridge = try bridge(lane, ["sub-a", "sub-c"])
        withExtendedLifetime(bridge) {}
        XCTAssertEqual(lane.fake.starts.count, 2)
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.identity.streamUUID, Self.uuidA)
        XCTAssertEqual(lane.ledger.stream(deviceID: 2)?.identity.streamUUID, Self.uuidB)
        lane.fake.starts[1].deliver([event(900, "\(Self.base)/dev2/c/top.txt")])
        XCTAssertEqual(lane.ledger.stream(deviceID: 2)?.lastCoveredEventID, 900)
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.lastCoveredEventID, 100)
    }

    // MARK: - Start position

    func testKnownStreamResumesFromCoveredIDOnlyWhenUUIDMatches() throws {
        let lane = try lane()
        var first: FolderEventBridge? = try bridge(lane)
        lane.fake.starts[0].deliver([event(150, "\(Self.base)/a/x.txt")])
        first = nil
        XCTAssertEqual(lane.fake.starts[0].handle.stops.count, 1)
        let epoch = lane.ledger.stream(deviceID: 1)?.observationEpoch

        lane.fake.current = 200
        let second = try bridge(lane)
        XCTAssertEqual(lane.fake.starts[1].since, 150)
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.observationEpoch, epoch)
        withExtendedLifetime((first, second)) {}
    }

    func testChangedMissingOrFutureHistoryOpensANewEpochWithFullReconciliation() throws {
        let cases: [(String, (Fake) -> Void)] = [
            ("changed uuid", { $0.uuids[1] = Self.uuidB }),
            ("missing uuid", { $0.uuids[1] = .some(nil) }),
            ("covered id beyond the stream", { $0.current = 120 }),
        ]
        for (name, change) in cases {
            let lane = try lane()
            var first: FolderEventBridge? = try bridge(lane)
            lane.fake.starts[0].deliver([event(150, "\(Self.base)/a/x.txt")])
            first = nil
            try settle(lane)
            let epoch = try XCTUnwrap(lane.ledger.stream(deviceID: 1)).observationEpoch
            lane.fake.current = 300
            change(lane.fake)
            let signalsBefore = lane.signals.all.count
            let second = try bridge(lane)
            let since = lane.fake.starts[1].since
            XCTAssertEqual(since, lane.fake.current, name)
            XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.observationEpoch, epoch + 1, name)
            XCTAssertNotNil(lane.ledger.obligation(subscriptionID: "sub-a")?.fullRevision, name)
            XCTAssertNotNil(lane.ledger.obligation(subscriptionID: "sub-b")?.fullRevision, name)
            XCTAssertEqual(lane.signals.all.count, signalsBefore + 1, name)
            XCTAssertTrue(lane.signals.all.last?.full == true, name)
            withExtendedLifetime((first, second)) {}
        }
    }

    // MARK: - Flags

    func testDroppedRootChangedAndScanFlagsBecomeFullReconciliationForAffectedRoots() throws {
        let cases: [(String, UInt32, String, Set<String>, FolderObservationReason)] = [
            ("scan inside one root", UInt32(kFSEventStreamEventFlagMustScanSubDirs), "\(Self.base)/a/deep", ["sub-a"], .mustScanSubDirs),
            ("user dropped", UInt32(kFSEventStreamEventFlagUserDropped), "", ["sub-a", "sub-b"], .eventsDropped),
            ("kernel dropped", UInt32(kFSEventStreamEventFlagKernelDropped), "", ["sub-a", "sub-b"], .eventsDropped),
            ("one root changed", UInt32(kFSEventStreamEventFlagRootChanged), "\(Self.base)/b", ["sub-b"], .rootChanged),
            ("unattributed root change", UInt32(kFSEventStreamEventFlagRootChanged), "/nowhere", ["sub-a", "sub-b"], .rootChanged),
        ]
        for (name, flags, path, expected, reason) in cases {
            let lane = try lane()
            let bridge = try bridge(lane)
            try settle(lane)
            lane.fake.starts[0].deliver([event(150, path, flags)])
            withExtendedLifetime(bridge) {}
            for id in ["sub-a", "sub-b"] {
                let obligation = lane.ledger.obligation(subscriptionID: id)
                if expected.contains(id) {
                    XCTAssertNotNil(obligation?.fullRevision, "\(name) \(id)")
                    XCTAssertEqual(obligation?.reasons, [reason], "\(name) \(id)")
                } else {
                    XCTAssertNil(obligation, "\(name) \(id)")
                }
            }
            XCTAssertEqual(lane.signals.all.last?.ids, expected, name)
            XCTAssertTrue(lane.signals.all.last?.full == true, name)
        }
    }

    func testWrappedFlagAndRegressedIDsOpenANewEpoch() throws {
        let lane = try lane()
        let bridge = try bridge(lane)
        lane.fake.starts[0].deliver([event(200, "\(Self.base)/a/x.txt")])
        try settle(lane)
        let epoch = try XCTUnwrap(lane.ledger.stream(deviceID: 1)).observationEpoch

        // Lower than the cursor, no flag: the history is not the one the cursor describes.
        lane.fake.starts[0].deliver([event(50, "\(Self.base)/a/y.txt")])
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.observationEpoch, epoch + 1)
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.lastCoveredEventID, 50)
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: "sub-b")?.fullRevision)
        XCTAssertEqual(lane.signals.all.last?.ids, ["sub-a", "sub-b"])

        try settle(lane)
        lane.fake.starts[0].deliver([event(60, "", UInt32(kFSEventStreamEventFlagEventIdsWrapped))])
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.observationEpoch, epoch + 2)
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: "sub-a")?.fullRevision)
        XCTAssertTrue(lane.signals.all.last?.full == true)
        withExtendedLifetime(bridge) {}
    }

    func testEventIDsArePersistedInOrderAndTheCursorNeverMovesBack() throws {
        let lane = try lane()
        let bridge = try bridge(lane)
        lane.fake.starts[0].deliver([event(310, "\(Self.base)/a/late.txt"), event(305, "\(Self.base)/a/early.txt")])
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.lastCoveredEventID, 310)
        lane.fake.starts[0].deliver([event(310, "\(Self.base)/a/late.txt")])
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.lastCoveredEventID, 310)
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.observationEpoch, 1)
        withExtendedLifetime(bridge) {}
    }

    func testHistoryDoneNeverClearsObligationsOrSignals() throws {
        let lane = try lane()
        let bridge = try bridge(lane)
        let before = lane.ledger.obligation(subscriptionID: "sub-a")
        let signals = lane.signals.all.count
        let history = UInt32(kFSEventStreamEventFlagHistoryDone)
        lane.fake.starts[0].deliver([event(0, "", history)])
        XCTAssertEqual(lane.ledger.obligation(subscriptionID: "sub-a"), before)
        XCTAssertEqual(lane.signals.all.count, signals)
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.lastCoveredEventID, 100)

        lane.fake.starts[0].deliver([event(0, "", history), event(170, "\(Self.base)/a/x.txt")])
        XCTAssertNotNil(lane.ledger.obligation(subscriptionID: "sub-a")?.fullRevision)
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.lastCoveredEventID, 170)
        withExtendedLifetime(bridge) {}
    }

    // MARK: - Storage failure

    func testLedgerWriteFailureStopsTheObserverAndNeverSignals() throws {
        let lane = try lane()
        let attention = AttentionLog()
        let bridge = try bridge(lane, onAttention: attention.record)
        let signals = lane.signals.all.count
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o500], ofItemAtPath: lane.directory.appendingPathComponent("observation").path
        )
        lane.fake.starts[0].deliver([event(150, "\(Self.base)/a/x.txt")])
        XCTAssertEqual(lane.signals.all.count, signals)
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.lastCoveredEventID, 100)
        XCTAssertEqual(lane.fake.starts[0].handle.stops.count, 1)
        XCTAssertEqual(attention.values, [.storage(deviceID: 1)])
        XCTAssertEqual(bridge.attention, [.storage(deviceID: 1)])

        // Even once storage recovers this observer stays off until it is rebuilt.
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o700], ofItemAtPath: lane.directory.appendingPathComponent("observation").path
        )
        lane.fake.starts[0].deliver([event(151, "\(Self.base)/a/y.txt")])
        XCTAssertEqual(lane.signals.all.count, signals)
        XCTAssertEqual(lane.ledger.stream(deviceID: 1)?.lastCoveredEventID, 100)
    }

    func testStartupLedgerFailureStartsNoStream() throws {
        let lane = try lane()
        let attention = AttentionLog()
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o500], ofItemAtPath: lane.directory.appendingPathComponent("observation").path
        )
        let bridge = try bridge(lane, onAttention: attention.record)
        XCTAssertTrue(lane.fake.starts.isEmpty)
        XCTAssertTrue(lane.signals.all.isEmpty)
        XCTAssertEqual(attention.values, [.storage(deviceID: 1)])
        withExtendedLifetime(bridge) {}
    }

    func testStreamThatCannotStartIsReported() throws {
        let lane = try lane()
        lane.fake.failStart = true
        let attention = AttentionLog()
        let bridge = try bridge(lane, onAttention: attention.record)
        XCTAssertEqual(attention.values, [.streamUnavailable(deviceID: 1)])
        XCTAssertTrue(lane.signals.all.isEmpty)
        withExtendedLifetime(bridge) {}
    }

    private final class AttentionLog: @unchecked Sendable {
        private let lock = NSLock()
        private var seen: [FolderObservationAttention] = []
        var values: [FolderObservationAttention] { lock.withLock { seen } }
        var record: @Sendable (FolderObservationAttention) -> Void {
            { [self] value in lock.withLock { seen.append(value) } }
        }
    }

    // MARK: - Live stream

    func testLiveStreamPersistsARelativeScopeBeforeSignalling() throws {
        guard ProcessInfo.processInfo.environment["OMNIVIA_RUN_FSEVENTS_QUALIFICATION"] == "1" else {
            throw XCTSkip(
                "host FSEvents delivery is a macOS qualification check; set "
                    + "OMNIVIA_RUN_FSEVENTS_QUALIFICATION=1 to run it"
            )
        }
        let lane = try lane()
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-bridge-live-\(UUID().uuidString)", isDirectory: true)
        let nested = root.appendingPathComponent("nested", isDirectory: true)
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let received = expectation(description: "persisted obligation covering the leaf signalled")
        received.assertForOverFulfill = false
        let ledger = lane.ledger
        // The real stream reports the watched root and `nested` as directory events too, so the
        // leaf write can arrive already coalesced into a full reconciliation or a covering
        // subtree. Anything that safely covers the leaf counts; a full obligation only counts
        // when an event caused it, since a fresh epoch owes one before the stream delivers
        // anything.
        let leaf = FolderObservationScope(kind: .member, components: ["nested", "note.txt"])
        let bridge = try XCTUnwrap(
            FolderEventBridge(
                roots: [FolderObservationRoot(subscriptionID: "sub-a", url: root)],
                ledger: ledger, subscriptions: lane.store
            ) { _ in
                // Read through the ledger: a signal raised before the write landed finds nothing.
                guard let obligation = ledger.obligation(subscriptionID: "sub-a") else { return }
                let covered = obligation.fullRevision != nil
                    ? obligation.reasons.contains(where: \.reportedByEvents)
                    : obligation.scopes.contains { $0.scope.covers(leaf) }
                if covered { received.fulfill() }
            }
        )
        try settle(lane, ["sub-a"])
        withExtendedLifetime(bridge) {
            DispatchQueue.global().asyncAfter(deadline: .now() + 0.3) {
                try? Data("changed".utf8).write(to: nested.appendingPathComponent("note.txt"))
            }
            wait(for: [received], timeout: 8)
        }
        XCTAssertGreaterThan(try XCTUnwrap(ledger.streams().first).lastCoveredEventID, 0)
        let state = try String(contentsOf: lane.stateFile, encoding: .utf8)
        XCTAssertFalse(state.contains(root.lastPathComponent))
        XCTAssertFalse(state.contains(root.standardizedFileURL.path))
    }
}
