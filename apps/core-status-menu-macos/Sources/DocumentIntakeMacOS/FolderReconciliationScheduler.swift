import Darwin
import Foundation

public enum FolderReconciliationError: Error, Equatable {
    case invalidState
    case unsupportedSchema
    case stateUnavailable
    case tooLarge
    case unknownSubscription
    /// `.due` is what the schedule decides; a caller asks for one of the immediate triggers.
    case invalidTrigger
}

/// The two kinds of periodic work a folder subscription owes (design §21.6). They are tracked
/// separately because metadata is cheap and frequent while content verification is neither.
public enum FolderReconciliationKind: String, Codable, CaseIterable, Sendable {
    /// Re-read the watched scope's membership and metadata.
    case metadata
    /// Re-verify the content of eligible members whose bytes are already local.
    case content

    /// How long after a completed pass this kind becomes due again, before jitter.
    public var interval: TimeInterval {
        switch self {
        case .metadata: 15 * 60
        case .content: 24 * 60 * 60
        }
    }
}

/// Why work is owed. Everything other than `.due` is an immediate trigger: it makes work owed now
/// regardless of where the interval stands.
public enum FolderReconciliationTrigger: String, Codable, CaseIterable, Sendable {
    /// The interval elapsed.
    case due
    case startup
    case wake
    case remount
    /// The owner pressed Resume.
    case resume
    case sourceAccessRecovered = "source_access_recovered"
    /// The event stream's identity or history is no longer comparable, so nothing can be assumed
    /// covered. `FolderObservationLedger` and `FolderEventBridge` decide this; see `observe(_:)`.
    case observationLost = "observation_lost"

    public var isImmediate: Bool { self != .due }
}

/// What one finished pass actually covered. Only `.complete` may be read as "up to date"; a due
/// interval on its own promises nothing about coverage.
public enum FolderReconciliationOutcome: String, Codable, Sendable {
    case complete
    /// Some eligible items were covered and some were not.
    case partial
    /// Nothing could be covered: the volume or file provider backing the root was not available.
    case providerUnavailable = "provider_unavailable"
    /// The pass did not finish.
    case failed
}

/// What a pass reports back. The scheduler derives the outcome from these facts rather than taking
/// "up to date" on trust.
public struct FolderReconciliationResult: Equatable, Sendable {
    /// Eligible items this kind of work fully covered.
    public let examined: Int
    /// Eligible items skipped because their bytes are not locally resident. Residency is a
    /// separate consent, so these are never downloaded to close the gap — they stay uncovered,
    /// and visibly so.
    public let notResident: Int
    /// Eligible items that should have been covered and could not be.
    public let unreadable: Int
    /// False when the volume or file provider behind the root was not available at all, in which
    /// case nothing could be covered and all three counts must be zero.
    public let providerAvailable: Bool
    /// The pass threw; the counts describe only what it had reached.
    public let failed: Bool

    public init(
        examined: Int,
        notResident: Int = 0,
        unreadable: Int = 0,
        providerAvailable: Bool = true,
        failed: Bool = false
    ) {
        self.examined = examined
        self.notResident = notResident
        self.unreadable = unreadable
        self.providerAvailable = providerAvailable
        self.failed = failed
    }

    public var outcome: FolderReconciliationOutcome {
        if failed { return .failed }
        guard providerAvailable else { return .providerUnavailable }
        return unreadable > 0 || notResident > 0 ? .partial : .complete
    }
}

/// One subscription's durable coverage truth for one kind of work. Time lives in the caller: every
/// question takes `now`, so this value is pure.
public struct FolderReconciliationCoverageState: Equatable, Sendable {
    public let subscriptionID: String
    public let kind: FolderReconciliationKind
    /// When this kind becomes due again. Nil means it has never completed a pass, so it is due now.
    public let dueAt: Date?
    public let lastAttemptAt: Date?
    /// When this kind last covered the whole scope. It stays nil until an outcome of `.complete`:
    /// a due interval is not a promise that complete coverage ever happened.
    public let lastCompleteAt: Date?
    public let lastOutcome: FolderReconciliationOutcome?
    public let examined: Int
    public let notResident: Int
    public let unreadable: Int
    /// Set while work is owed and has not been reported finished. It survives restart.
    public let pendingTrigger: FolderReconciliationTrigger?

    public func isDue(at now: Date) -> Bool {
        guard pendingTrigger == nil else { return true }
        guard let dueAt else { return true }
        return now >= dueAt
    }

    /// Work is owed and the last pass did not achieve complete coverage, so there is no recent
    /// truth to fall back on. The UI shows attention here, never "up to date".
    public func isOverdue(at now: Date) -> Bool {
        isDue(at: now) && lastOutcome != .complete
    }

    /// The only state a UI may present as up to date.
    public func isUpToDate(at now: Date) -> Bool {
        pendingTrigger == nil && lastOutcome == .complete && !isDue(at: now)
    }

    /// Items the last pass left uncovered. Non-zero means coverage is partial whatever the clock
    /// says.
    public var uncovered: Int { notResident + unreadable }
}

private struct FolderReconciliationEntry: Codable, Equatable {
    var subscriptionID: String
    var kind: FolderReconciliationKind
    var dueAt: Double?
    var lastAttemptAt: Double?
    var lastCompleteAt: Double?
    var lastOutcome: FolderReconciliationOutcome?
    var examined: Int
    var notResident: Int
    var unreadable: Int
    var pendingTrigger: FolderReconciliationTrigger?
}

private struct FolderReconciliationDocument: Codable {
    var schema: String
    var entries: [FolderReconciliationEntry]
}

private struct FolderReconciliationHeader: Codable {
    let schema: String
}

/// Decides when an active folder subscription owes periodic reconciliation, and records truthfully
/// what the last pass covered.
///
/// It schedules work; it never reads a watched scope. Scheduling a kind of work merges a durable
/// full-reconciliation obligation into `FolderObservationLedger` without touching that stream's
/// event cursor, so a schedule can never be mistaken for evidence that an event was covered.
///
/// Paused, stopped and not-yet-active subscriptions are never scheduled: no automatic metadata or
/// content read is armed for them at all.
public final class FolderReconciliationScheduler: @unchecked Sendable {
    public struct Demand: Equatable, Sendable {
        public let subscriptionID: String
        public let kind: FolderReconciliationKind
        public let trigger: FolderReconciliationTrigger
    }

    static let schema = "omnivia.folder-reconciliation.v1"
    static let maximumStateBytes = 1024 * 1024
    static let maximumEntries = 512
    /// The three counts partition one eligible population, so their *total* is what the design's
    /// eligible-file bound limits. Refusing a larger total is what keeps `uncovered` from
    /// overflowing and keeps a pass from claiming more items than the scope can hold.
    static let maximumCount = FolderScopeLimits.standard.maximumEligibleFiles
    /// Design §21.6: a due interval may be stretched by up to this fraction, deterministically, so
    /// subscriptions established together do not all come due in the same instant.
    public static let maximumJitterFraction = 0.2

    private let gate: PublicationGate
    private let subscriptions: FolderSubscriptionStore
    private let observation: FolderObservationLedger
    private let clock: @Sendable () -> Date
    private let jitter: @Sendable (String, FolderReconciliationKind) -> Double
    private let directory: URL
    private let stateFile: URL
    private var document: FolderReconciliationDocument

    public init(
        stateDirectory: URL,
        subscriptions: FolderSubscriptionStore,
        observation: FolderObservationLedger,
        clock: @escaping @Sendable () -> Date = { Date() },
        jitter: @escaping @Sendable (String, FolderReconciliationKind) -> Double
            = FolderReconciliationScheduler.deterministicJitter,
        gate: PublicationGate = .processShared
    ) throws {
        self.gate = gate
        self.subscriptions = subscriptions
        self.observation = observation
        self.clock = clock
        self.jitter = jitter
        directory = stateDirectory
        stateFile = stateDirectory.appendingPathComponent("folder-reconciliation-v1.json")
        try Self.prepareDirectory(stateDirectory)
        document = try Self.load(stateFile)
            ?? FolderReconciliationDocument(schema: Self.schema, entries: [])
    }

    /// A stable fraction in `0..<1` per subscription and kind. Pure, so the same subscription keeps
    /// its slot in the window across restarts instead of drifting towards its neighbours.
    public static let deterministicJitter: @Sendable (String, FolderReconciliationKind) -> Double = {
        subscriptionID, kind in
        var hash: UInt64 = 0xcbf2_9ce4_8422_2325
        for byte in (subscriptionID + "\u{0}" + kind.rawValue).utf8 {
            hash = (hash ^ UInt64(byte)) &* 0x100_0000_01b3
        }
        return Double(hash % 10_000) / 10_000
    }

    /// `dueAt` for a pass that completed at `from`. An injected fraction outside `0...1` is clamped,
    /// so no caller can push the next due instant past the design's bound.
    static func nextDue(
        _ kind: FolderReconciliationKind,
        from: Date,
        fraction: Double
    ) -> Date {
        let clamped = fraction.isFinite ? min(max(fraction, 0), 1) : 0
        return from.addingTimeInterval(kind.interval * (1 + maximumJitterFraction * clamped))
    }

    // MARK: - Reading

    /// Both kinds for one subscription, including kinds that have never run.
    public func coverage(subscriptionID: String) -> [FolderReconciliationCoverageState] {
        gate.withLock {
            FolderReconciliationKind.allCases.map {
                Self.state(subscriptionID, $0, entry(subscriptionID, $0, in: document))
            }
        }
    }

    public func coverage(
        subscriptionID: String,
        kind: FolderReconciliationKind
    ) -> FolderReconciliationCoverageState {
        gate.withLock { Self.state(subscriptionID, kind, entry(subscriptionID, kind, in: document)) }
    }

    /// What is owed right now, without arming anything or writing anything.
    public func due() -> [Demand] {
        let now = clock()
        return gate.withLock { demands(at: now).all }
    }

    // MARK: - Scheduling

    /// Arms everything the schedule now owes and returns it, newly armed and already-armed alike.
    /// Already-armed work is returned without merging a second obligation: the first one is still
    /// outstanding, which is what makes this safe to call on every tick and after a restart.
    @discardableResult
    public func schedule() throws -> [Demand] {
        let now = clock()
        let split = gate.withLock { demands(at: now) }
        try arm(split.fresh)
        return split.all
    }

    /// Records an immediate trigger — startup, wake, remount, Resume, recovered source access, lost
    /// observation — for the active subscriptions among `subscriptionIDs`. A kind that is already
    /// armed keeps the trigger it has: the outstanding full reconciliation covers this one too.
    @discardableResult
    public func request(
        subscriptionIDs: Set<String>,
        trigger: FolderReconciliationTrigger
    ) throws -> [Demand] {
        guard trigger.isImmediate else { throw FolderReconciliationError.invalidTrigger }
        let active = Set(
            subscriptions.all().filter { $0.state == .active }.map(\.subscriptionID)
        ).intersection(subscriptionIDs)
        let fresh = gate.withLock {
            active.sorted().flatMap { id in
                FolderReconciliationKind.allCases.compactMap { kind in
                    entry(id, kind, in: document)?.pendingTrigger == nil
                        ? Demand(subscriptionID: id, kind: kind, trigger: trigger)
                        : nil
                }
            }
        }
        try arm(fresh)
        return fresh
    }

    /// The narrow seam onto `FolderEventBridge`. Stream UUID and epoch invalidation are the
    /// ledger's and the bridge's to decide; this reads only the one bit they publish and does not
    /// re-derive any of it. A signal that needs no full work schedules nothing: the event
    /// obligations the bridge already persisted are the work.
    @discardableResult
    public func observe(_ signal: FolderEventSignal) throws -> [Demand] {
        guard signal.requiresFullReconciliation, !signal.subscriptionIDs.isEmpty else { return [] }
        return try request(subscriptionIDs: signal.subscriptionIDs, trigger: .observationLost)
    }

    /// Records what a pass covered, clears its armed trigger and sets the next due instant. The
    /// outcome comes from `result`'s own facts, so a pass that left items uncovered cannot be
    /// recorded as complete coverage.
    @discardableResult
    public func complete(
        subscriptionID: String,
        kind: FolderReconciliationKind,
        result: FolderReconciliationResult
    ) throws -> FolderReconciliationCoverageState {
        guard subscriptions.subscription(id: subscriptionID) != nil else {
            throw FolderReconciliationError.unknownSubscription
        }
        guard Self.withinEligibleBound(result.examined, result.notResident, result.unreadable),
              // An unavailable provider covered, skipped and failed on nothing at all.
              result.providerAvailable
                  || (result.examined == 0 && result.notResident == 0 && result.unreadable == 0)
        else { throw FolderReconciliationError.invalidState }
        let now = clock()
        let outcome = result.outcome
        let due = Self.nextDue(kind, from: now, fraction: jitter(subscriptionID, kind))
        return try mutate { document in
            var value = entry(subscriptionID, kind, in: document)
                ?? FolderReconciliationEntry(
                    subscriptionID: subscriptionID, kind: kind, examined: 0, notResident: 0, unreadable: 0
                )
            value.dueAt = due.timeIntervalSince1970
            value.lastAttemptAt = now.timeIntervalSince1970
            if outcome == .complete { value.lastCompleteAt = now.timeIntervalSince1970 }
            value.lastOutcome = outcome
            value.examined = result.examined
            value.notResident = result.notResident
            value.unreadable = result.unreadable
            value.pendingTrigger = nil
            Self.replace(value, in: &document)
            return Self.state(subscriptionID, kind, value)
        }
    }

    // MARK: - Internals

    private func demands(at now: Date) -> (fresh: [Demand], all: [Demand]) {
        var fresh: [Demand] = []
        var all: [Demand] = []
        let active = subscriptions.all().filter { $0.state == .active }.map(\.subscriptionID)
        for id in active.sorted() {
            for kind in FolderReconciliationKind.allCases {
                let value = entry(id, kind, in: document)
                if let pending = value?.pendingTrigger {
                    all.append(Demand(subscriptionID: id, kind: kind, trigger: pending))
                } else if Self.state(id, kind, value).isDue(at: now) {
                    let demand = Demand(subscriptionID: id, kind: kind, trigger: .due)
                    fresh.append(demand)
                    all.append(demand)
                }
            }
        }
        return (fresh, all)
    }

    /// Merges the durable full-reconciliation obligation first, then records the armed triggers. A
    /// failure between the two leaves an obligation this process has not armed, which is extra work
    /// rather than lost work; a failed record leaves the prior durable state exactly as it was.
    private func arm(_ demands: [Demand]) throws {
        guard !demands.isEmpty else { return }
        // One gate for both writes, so nothing observes a merged obligation that is not yet armed,
        // or a completion that lands between the two.
        try gate.withLock {
            for id in Set(demands.map(\.subscriptionID)).sorted() {
                let immediate = demands.contains { $0.subscriptionID == id && $0.trigger.isImmediate }
                do {
                    try observation.requestFullReconciliation(
                        subscriptionID: id,
                        reason: immediate ? .immediate : .scheduled
                    )
                } catch FolderObservationLedgerError.unknownSubscription {
                    // No stream observes it yet. `beginStream` owes the full reconciliation itself,
                    // so there is nothing to merge into and nothing lost.
                }
                // Every other ledger failure, `.closed` included, propagates and arms nothing. A
                // clean checkpoint means the next open demands no catch-up, so an obligation that
                // was never merged would never be re-derived — and an already-armed entry does not
                // retry the merge. Failing here keeps the work due and retriable instead.
            }
            try mutate { document in
                for demand in demands {
                    var value = entry(demand.subscriptionID, demand.kind, in: document)
                        ?? FolderReconciliationEntry(
                            subscriptionID: demand.subscriptionID, kind: demand.kind,
                            examined: 0, notResident: 0, unreadable: 0
                        )
                    guard value.pendingTrigger == nil else { continue }
                    value.pendingTrigger = demand.trigger
                    Self.replace(value, in: &document)
                }
            }
        }
    }

    private func entry(
        _ subscriptionID: String,
        _ kind: FolderReconciliationKind,
        in document: FolderReconciliationDocument
    ) -> FolderReconciliationEntry? {
        document.entries.first { $0.subscriptionID == subscriptionID && $0.kind == kind }
    }

    private static func replace(
        _ value: FolderReconciliationEntry,
        in document: inout FolderReconciliationDocument
    ) {
        document.entries.removeAll { $0.subscriptionID == value.subscriptionID && $0.kind == value.kind }
        document.entries.append(value)
        document.entries.sort { ($0.subscriptionID, $0.kind.rawValue) < ($1.subscriptionID, $1.kind.rawValue) }
    }

    private static func state(
        _ subscriptionID: String,
        _ kind: FolderReconciliationKind,
        _ entry: FolderReconciliationEntry?
    ) -> FolderReconciliationCoverageState {
        FolderReconciliationCoverageState(
            subscriptionID: subscriptionID,
            kind: kind,
            dueAt: entry?.dueAt.map { Date(timeIntervalSince1970: $0) },
            lastAttemptAt: entry?.lastAttemptAt.map { Date(timeIntervalSince1970: $0) },
            lastCompleteAt: entry?.lastCompleteAt.map { Date(timeIntervalSince1970: $0) },
            lastOutcome: entry?.lastOutcome,
            examined: entry?.examined ?? 0,
            notResident: entry?.notResident ?? 0,
            unreadable: entry?.unreadable ?? 0,
            pendingTrigger: entry?.pendingTrigger
        )
    }

    private func mutate<T>(_ change: (inout FolderReconciliationDocument) throws -> T) throws -> T {
        try gate.withLock {
            var next = document
            let result = try change(&next)
            try Self.validate(next)
            try Self.persist(next, to: stateFile, directory: directory)
            document = next
            return result
        }
    }

    // MARK: - Validation

    private static func validate(_ document: FolderReconciliationDocument) throws {
        guard document.schema == schema, document.entries.count <= maximumEntries else {
            throw FolderReconciliationError.invalidState
        }
        var seen = Set<String>()
        for entry in document.entries {
            guard validIdentifier(entry.subscriptionID),
                  seen.insert(entry.subscriptionID + "\u{0}" + entry.kind.rawValue).inserted,
                  withinEligibleBound(entry.examined, entry.notResident, entry.unreadable),
                  // Provider availability survives only as the outcome, so that is where the
                  // "nothing could be covered" rule lands on the way back in. Complete, partial
                  // and failed entries keep whatever counts they recorded.
                  entry.lastOutcome != .providerUnavailable
                      || (entry.examined == 0 && entry.notResident == 0 && entry.unreadable == 0),
                  [entry.dueAt, entry.lastAttemptAt, entry.lastCompleteAt].allSatisfy({ $0?.isFinite ?? true }),
                  // Complete coverage is the only thing that may claim a completion instant.
                  (entry.lastCompleteAt == nil) || (entry.lastAttemptAt != nil),
                  (entry.lastCompleteAt == nil) || (entry.lastOutcome != nil)
            else { throw FolderReconciliationError.invalidState }
        }
        let keys = document.entries.map { ($0.subscriptionID, $0.kind.rawValue) }
        guard keys.indices.dropFirst().allSatisfy({ keys[$0 - 1] < keys[$0] }) else {
            throw FolderReconciliationError.invalidState
        }
    }

    /// The counts are non-negative and together describe no more items than one scope may hold.
    /// Summed with overflow reporting, because a tampered document or a buggy pass can hand over
    /// values whose sum does not fit an `Int`.
    static func withinEligibleBound(_ examined: Int, _ notResident: Int, _ unreadable: Int) -> Bool {
        guard examined >= 0, notResident >= 0, unreadable >= 0 else { return false }
        let (pair, pairOverflowed) = examined.addingReportingOverflow(notResident)
        guard !pairOverflowed else { return false }
        let (total, totalOverflowed) = pair.addingReportingOverflow(unreadable)
        return !totalOverflowed && total <= maximumCount
    }

    private static func validIdentifier(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= 128
            && value.unicodeScalars.allSatisfy {
                CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
                    .contains($0)
            }
    }

    // MARK: - Persistence

    private static func load(_ stateFile: URL) throws -> FolderReconciliationDocument? {
        guard FileManager.default.fileExists(atPath: stateFile.path) else { return nil }
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: stateFile.path)
            guard (attributes[.type] as? FileAttributeType) == .typeRegular,
                  let size = attributes[.size] as? NSNumber,
                  size.uint64Value <= UInt64(maximumStateBytes),
                  let owner = attributes[.ownerAccountID] as? NSNumber,
                  owner.uint32Value == Darwin.getuid(),
                  let permissions = attributes[.posixPermissions] as? NSNumber,
                  permissions.intValue & 0o077 == 0
            else { throw FolderReconciliationError.invalidState }
            let data = try Data(contentsOf: stateFile)
            // A newer build's document is refused by schema before its shape is judged.
            let header = try JSONDecoder().decode(FolderReconciliationHeader.self, from: data)
            guard header.schema == schema else { throw FolderReconciliationError.unsupportedSchema }
            let document = try JSONDecoder().decode(FolderReconciliationDocument.self, from: data)
            try validate(document)
            return document
        } catch let error as FolderReconciliationError {
            throw error
        } catch {
            throw FolderReconciliationError.invalidState
        }
    }

    private static func persist(
        _ document: FolderReconciliationDocument,
        to stateFile: URL,
        directory: URL
    ) throws {
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
            let data = try encoder.encode(document)
            guard data.count <= maximumStateBytes else { throw FolderReconciliationError.tooLarge }
            let temporary = directory.appendingPathComponent(".folder-reconciliation-\(UUID().uuidString).tmp")
            try data.write(to: temporary, options: [.atomic])
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: temporary.path)
            if FileManager.default.fileExists(atPath: stateFile.path) {
                _ = try FileManager.default.replaceItemAt(stateFile, withItemAt: temporary)
            } else {
                try FileManager.default.moveItem(at: temporary, to: stateFile)
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateFile.path)
            let descriptor = Darwin.open(directory.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
            if descriptor >= 0 {
                _ = Darwin.fsync(descriptor)
                Darwin.close(descriptor)
            }
        } catch let error as FolderReconciliationError {
            throw error
        } catch {
            throw FolderReconciliationError.stateUnavailable
        }
    }

    private static func prepareDirectory(_ directory: URL) throws {
        do {
            try FileManager.default.createDirectory(
                at: directory,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let values = try directory.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
            guard values.isDirectory == true, values.isSymbolicLink != true else {
                throw FolderReconciliationError.stateUnavailable
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        } catch let error as FolderReconciliationError {
            throw error
        } catch {
            throw FolderReconciliationError.stateUnavailable
        }
    }
}
