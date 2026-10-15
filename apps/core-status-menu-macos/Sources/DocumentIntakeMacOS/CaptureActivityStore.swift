import Darwin
import Foundation

/// What is proven about one submitted item on this Mac.
///
/// These are separate facts, not stages of one collapsed progress enum.
/// `queued` never means added, and `processing` means only that Core accepted
/// source and job state and returned a capture id — it is not a claim that a
/// searchable publication, an extraction, retrieval readiness or a terminal
/// canonical receipt exists. This side holds no canonical capture or extraction
/// history at all.
public enum CaptureActivityFact: String, Codable, Equatable, Sendable {
    /// A frozen folder manifest names this member. No bytes are staged for it yet.
    case planned
    /// Manifest and private payload are durable locally. Nothing has been sent.
    case queued
    /// An externally visible attempt is in flight.
    case sending
    /// An attempt may already have committed; its outcome is unknown.
    case checking
    /// Core accepted source and job state and returned a capture id. The
    /// addition is not finished: no searchable publication, extraction,
    /// retrieval readiness or terminal canonical receipt is proven here, and
    /// this side will never learn of one. It is therefore unresolved work that
    /// nothing local may clear, age out or count against a retention bound.
    case processing
    /// A known failed outcome for this item.
    case failed
    /// An unstarted member was cancelled before anything was sent.
    case stopped

    /// This item reached a local outcome: nothing further happens to it on this
    /// Mac without a new explicit action, and nothing further will be learned
    /// about it here. `processing` is deliberately not one — Core still owes it
    /// a publication and a terminal receipt this store cannot see — so it has
    /// no resolution time and no retention bound applies to it.
    ///
    /// Whether a resolved outcome is a receipt that may be cleared is a
    /// separate question — see `CaptureActivityRecord.isReceipt`.
    public var isResolved: Bool {
        switch self {
        case .planned, .queued, .sending, .checking, .processing: false
        case .failed, .stopped: true
        }
    }

    /// An unknown outcome needs a decision as much as a known failure does:
    /// neither resolves on its own. A run ledger that disagrees with the queue
    /// reaches `checking` for the same reason.
    public var needsAttention: Bool { self == .checking || self == .failed }
}

/// One item's local activity. Its identity fields are frozen when the item is
/// first observed and are never rewritten, so recovery after a restart names
/// exactly the same work and never allocates a replacement key.
public struct CaptureActivityRecord: Codable, Equatable, Sendable {
    /// The queue's own item id, which is also the folder manifest's member item
    /// id. This is the recovery identity.
    public let itemID: String
    public let submissionID: String
    /// The stable parent capture intent key. Attempt keys are derived from it.
    public let operationKey: String
    public let displayName: String
    /// The folder run this item belongs to, when one does. An ordinary
    /// submission groups by `submissionID` instead.
    public let runID: String?
    public let manifestRevision: String?
    public let memberID: String?
    /// Monotonic within this store, assigned once. Orders the surface and
    /// decides which terminal activity a retention bound drops first.
    public let sequence: UInt64
    public let firstObservedAt: String
    public let fact: CaptureActivityFact
    /// When this item's terminal fact was first observed here. Nil while the
    /// item is unresolved.
    public let resolvedAt: String?
    public let attemptGeneration: UInt64
    public let attemptOperationKey: String?
    public let attemptRequestDigest: String?
    public let attemptDescriptorDigest: String?
    public let failureCode: String?
    public let admittedCaptureID: String?

    /// A receipt is a resolved item no authority here can still act on. Only a
    /// receipt is bounded by retention or removed by clearing.
    ///
    /// An item Core accepted for processing is deliberately not one: acceptance
    /// is not a searchable publication, retrieval readiness or a terminal
    /// canonical receipt, and nothing here will ever see that receipt arrive, so
    /// dropping the record would present unfinished work as finished and then
    /// lose it.
    ///
    /// A known failure inside a folder run is deliberately not one either: that
    /// run can retry exactly that member under its frozen identity, so the
    /// record is the handle on unresolved work and must survive both. A failed
    /// ordinary submission is a receipt, because no authority can return it to
    /// pending work and its recovery copy and key stay in the queue regardless.
    public var isReceipt: Bool {
        switch fact {
        case .planned, .queued, .sending, .checking, .processing: false
        case .stopped: true
        case .failed: runID == nil
        }
    }

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case itemID
        case submissionID
        case operationKey
        case displayName
        case runID
        case manifestRevision
        case memberID
        case sequence
        case firstObservedAt
        case fact
        case resolvedAt
        case attemptGeneration
        case attemptOperationKey
        case attemptRequestDigest
        case attemptDescriptorDigest
        case failureCode
        case admittedCaptureID
    }

    public init(
        itemID: String,
        submissionID: String,
        operationKey: String,
        displayName: String,
        runID: String? = nil,
        manifestRevision: String? = nil,
        memberID: String? = nil,
        sequence: UInt64,
        firstObservedAt: String,
        fact: CaptureActivityFact,
        resolvedAt: String? = nil,
        attemptGeneration: UInt64 = 0,
        attemptOperationKey: String? = nil,
        attemptRequestDigest: String? = nil,
        attemptDescriptorDigest: String? = nil,
        failureCode: String? = nil,
        admittedCaptureID: String? = nil
    ) {
        self.itemID = itemID
        self.submissionID = submissionID
        self.operationKey = operationKey
        self.displayName = displayName
        self.runID = runID
        self.manifestRevision = manifestRevision
        self.memberID = memberID
        self.sequence = sequence
        self.firstObservedAt = firstObservedAt
        self.fact = fact
        self.resolvedAt = resolvedAt
        self.attemptGeneration = attemptGeneration
        self.attemptOperationKey = attemptOperationKey
        self.attemptRequestDigest = attemptRequestDigest
        self.attemptDescriptorDigest = attemptDescriptorDigest
        self.failureCode = failureCode
        self.admittedCaptureID = admittedCaptureID
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        itemID = try container.decode(String.self, forKey: .itemID)
        submissionID = try container.decode(String.self, forKey: .submissionID)
        operationKey = try container.decode(String.self, forKey: .operationKey)
        displayName = try container.decode(String.self, forKey: .displayName)
        runID = try container.decodeIfPresent(String.self, forKey: .runID)
        manifestRevision = try container.decodeIfPresent(String.self, forKey: .manifestRevision)
        memberID = try container.decodeIfPresent(String.self, forKey: .memberID)
        sequence = try container.decode(UInt64.self, forKey: .sequence)
        firstObservedAt = try container.decode(String.self, forKey: .firstObservedAt)
        fact = try container.decode(CaptureActivityFact.self, forKey: .fact)
        resolvedAt = try container.decodeIfPresent(String.self, forKey: .resolvedAt)
        attemptGeneration = try container.decode(UInt64.self, forKey: .attemptGeneration)
        attemptOperationKey = try container.decodeIfPresent(String.self, forKey: .attemptOperationKey)
        attemptRequestDigest = try container.decodeIfPresent(String.self, forKey: .attemptRequestDigest)
        attemptDescriptorDigest = try container.decodeIfPresent(
            String.self,
            forKey: .attemptDescriptorDigest
        )
        failureCode = try container.decodeIfPresent(String.self, forKey: .failureCode)
        admittedCaptureID = try container.decodeIfPresent(String.self, forKey: .admittedCaptureID)
    }

    fileprivate init(
        _ derived: CaptureActivityObservation,
        sequence: UInt64,
        firstObservedAt: String,
        resolvedAt: String?
    ) {
        itemID = derived.itemID
        submissionID = derived.submissionID
        operationKey = derived.operationKey
        displayName = derived.displayName
        runID = derived.runID
        manifestRevision = derived.manifestRevision
        memberID = derived.memberID
        self.sequence = sequence
        self.firstObservedAt = firstObservedAt
        fact = derived.fact
        self.resolvedAt = resolvedAt
        attemptGeneration = derived.attemptGeneration
        attemptOperationKey = derived.attemptOperationKey
        attemptRequestDigest = derived.attemptRequestDigest
        attemptDescriptorDigest = derived.attemptDescriptorDigest
        failureCode = derived.failureCode
        admittedCaptureID = derived.admittedCaptureID
    }
}

/// What the durable owners currently prove about one item, before it is merged
/// with whatever this store already recorded.
fileprivate struct CaptureActivityObservation {
    let itemID: String
    let submissionID: String
    let operationKey: String
    let displayName: String
    let runID: String?
    let manifestRevision: String?
    let memberID: String?
    let fact: CaptureActivityFact
    let attemptGeneration: UInt64
    let attemptOperationKey: String?
    let attemptRequestDigest: String?
    let attemptDescriptorDigest: String?
    let failureCode: String?
    let admittedCaptureID: String?
}

public enum CaptureActivityError: Error, Equatable {
    case invalidState
    case stateUnavailable
}

private struct CaptureActivityDocument: Codable {
    let schema: String
    let nextSequence: UInt64
    let records: [CaptureActivityRecord]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schema
        case nextSequence
        case records
    }

    init(schema: String, nextSequence: UInt64, records: [CaptureActivityRecord]) {
        self.schema = schema
        self.nextSequence = nextSequence
        self.records = records
    }

    init(from decoder: Decoder) throws {
        try rejectUnknownKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schema = try container.decode(String.self, forKey: .schema)
        nextSequence = try container.decode(UInt64.self, forKey: .nextSequence)
        records = try container.decode([CaptureActivityRecord].self, forKey: .records)
    }
}

/// Owner-only durable local activity for Recent additions.
///
/// It records only what the durable queue and folder-run ledgers already prove,
/// one record per frozen item identity, and it is the only writer of its own
/// file. It reads those owners and never writes them, so clearing or pruning
/// activity cannot touch queue recovery data, an original link, a running
/// watch, a frozen run manifest or anything canonical.
///
/// A record is created unless the item is already a receipt (see
/// `CaptureActivityRecord.isReceipt`): a receipt with no record was cleared or
/// pruned, and adopting it would undo that. Startup and every delivery tick
/// observe before anything is delivered, so an item is recorded while it is
/// still queued and every later outcome of it is therefore observable.
public final class CaptureActivityStore: @unchecked Sendable {
    /// At most this many receipts are retained, oldest dropped first.
    public static let maximumRetainedReceipts = 100
    /// A receipt older than this is dropped.
    public static let receiptRetention: TimeInterval = 30 * 24 * 60 * 60

    private static let schema = "omnivia.macos.capture-activity.v1"
    private static let maximumStateBytes = 8 * 1024 * 1024

    private let lock = NSLock()
    private let directory: URL
    private let stateFile: URL
    private var records: [String: CaptureActivityRecord]
    private var nextSequence: UInt64

    public init(stateDirectory: URL) throws {
        directory = stateDirectory
        stateFile = stateDirectory.appendingPathComponent("capture-activity-v1.json")
        try Self.prepareDirectory(stateDirectory)
        let loaded = try Self.load(stateFile)
        records = loaded.records
        nextSequence = loaded.nextSequence
    }

    /// Current activity, oldest first.
    public func all() -> [CaptureActivityRecord] {
        lock.withLock { records.values.sorted { $0.sequence < $1.sequence } }
    }

    public func record(itemID: String) -> CaptureActivityRecord? {
        lock.withLock { records[itemID] }
    }

    /// Records that clearing would remove.
    public func receiptCount() -> Int {
        lock.withLock { records.values.filter(\.isReceipt).count }
    }

    /// Records what the durable queue and folder-run ledgers now prove, then
    /// applies the receipt retention bound. Returns the current activity.
    @discardableResult
    public func observe(
        queue: [CaptureQueueEntry],
        runs: [FolderBulkRun],
        now: Date = Date()
    ) throws -> [CaptureActivityRecord] {
        try lock.withLock {
            let stamp = Self.formatter.string(from: now)
            let entries = Dictionary(
                queue.map { ($0.intent.itemId, $0) },
                uniquingKeysWith: { first, _ in first }
            )
            var observations: [CaptureActivityObservation] = []
            var seen: Set<String> = []
            // Folder-run members first and in manifest order, so a member keeps
            // its grouping facts once a queue entry exists for it as well.
            for run in runs {
                for member in run.members where seen.insert(member.itemID).inserted {
                    observations.append(
                        Self.observation(run: run, member: member, entry: entries[member.itemID])
                    )
                }
            }
            for entry in queue.sorted(by: { $0.intent.itemId < $1.intent.itemId })
            where seen.insert(entry.intent.itemId).inserted {
                observations.append(Self.observation(entry: entry))
            }

            var replacement = records
            var sequence = nextSequence
            for observation in observations {
                if let existing = replacement[observation.itemID] {
                    replacement[observation.itemID] = CaptureActivityRecord(
                        observation,
                        sequence: existing.sequence,
                        firstObservedAt: existing.firstObservedAt,
                        resolvedAt: Self.resolvedAt(existing: existing, observed: observation, stamp: stamp)
                    )
                } else {
                    let created = CaptureActivityRecord(
                        observation,
                        sequence: sequence,
                        firstObservedAt: stamp,
                        resolvedAt: observation.fact.isResolved ? stamp : nil
                    )
                    // A receipt this store holds no record for was already
                    // cleared or pruned. Adopting it would undo clearing and
                    // would state a resolution time that was never observed.
                    guard !created.isReceipt else { continue }
                    replacement[observation.itemID] = created
                    sequence += 1
                }
            }
            replacement = Self.pruned(replacement, now: now)
            if replacement != records || sequence != nextSequence {
                try persist(replacement, nextSequence: sequence)
                records = replacement
                nextSequence = sequence
            }
            return records.values.sorted { $0.sequence < $1.sequence }
        }
    }

    /// Removes receipts and nothing else. Unresolved work stays — including an
    /// item Core has only accepted for processing, an unknown outcome and a
    /// folder member its run can still retry — and no other owner is touched:
    /// the queue keeps every recovery copy and key, run manifests keep their
    /// frozen membership, and already added files stay linked in Core. Sequence
    /// numbering is not rewound, so a cleared item can never be confused with a
    /// later one.
    @discardableResult
    public func clearReceipts() throws -> Int {
        try lock.withLock {
            let remaining = records.filter { !$0.value.isReceipt }
            guard remaining.count != records.count else { return 0 }
            let removed = records.count - remaining.count
            try persist(remaining, nextSequence: nextSequence)
            records = remaining
            return removed
        }
    }

    /// When the outcome was observed: the moment this fact was first seen, kept
    /// unchanged while that same fact holds, and dropped again if the item
    /// returns to unresolved work.
    private static func resolvedAt(
        existing: CaptureActivityRecord,
        observed observation: CaptureActivityObservation,
        stamp: String
    ) -> String? {
        guard observation.fact.isResolved else { return nil }
        guard existing.fact == observation.fact else { return stamp }
        return existing.resolvedAt ?? stamp
    }

    private static func pruned(
        _ values: [String: CaptureActivityRecord],
        now: Date
    ) -> [String: CaptureActivityRecord] {
        var surviving = values
        let cutoff = now.addingTimeInterval(-receiptRetention)
        for (key, record) in surviving where record.isReceipt {
            guard let resolved = record.resolvedAt.flatMap(formatter.date(from:)),
                  resolved >= cutoff
            else {
                surviving.removeValue(forKey: key)
                continue
            }
        }
        let receipts = surviving.values
            .filter(\.isReceipt)
            .sorted { $0.sequence < $1.sequence }
        guard receipts.count > maximumRetainedReceipts else { return surviving }
        for record in receipts.prefix(receipts.count - maximumRetainedReceipts) {
            surviving.removeValue(forKey: record.itemID)
        }
        return surviving
    }

    /// A folder-run member. The queue decides what is proven about delivery;
    /// the frozen manifest supplies the grouping facts and the two states that
    /// exist before anything is staged.
    private static func observation(
        run: FolderBulkRun,
        member: FolderBulkRunMember,
        entry: CaptureQueueEntry?
    ) -> CaptureActivityObservation {
        let attempt = entry?.attempts.last
        return CaptureActivityObservation(
            itemID: member.itemID,
            submissionID: run.submissionID,
            operationKey: member.operationKey,
            displayName: member.displayName,
            runID: run.runID,
            manifestRevision: run.manifestRevision,
            memberID: member.memberID,
            fact: entry.map(fact) ?? memberFact(member),
            attemptGeneration: entry?.attemptGeneration ?? 0,
            attemptOperationKey: attempt?.operationKey,
            attemptRequestDigest: entry?.stageRequestDigest ?? attempt?.requestDigest,
            attemptDescriptorDigest: entry?.stageDescriptorDigest ?? attempt?.descriptorDigest,
            failureCode: entry?.failureCode ?? member.failureCode,
            admittedCaptureID: entry?.admittedCaptureID
        )
    }

    private static func observation(entry: CaptureQueueEntry) -> CaptureActivityObservation {
        let attempt = entry.attempts.last
        return CaptureActivityObservation(
            itemID: entry.intent.itemId,
            submissionID: entry.intent.submissionId,
            operationKey: entry.intent.operationKey,
            displayName: entry.intent.displayName ?? entry.intent.itemId,
            runID: entry.intent.bulkOrigin?.bulkRunId,
            manifestRevision: entry.intent.bulkOrigin?.manifestRevision,
            memberID: entry.intent.bulkOrigin?.memberId,
            fact: fact(entry),
            attemptGeneration: entry.attemptGeneration,
            attemptOperationKey: attempt?.operationKey,
            attemptRequestDigest: entry.stageRequestDigest ?? attempt?.requestDigest,
            attemptDescriptorDigest: entry.stageDescriptorDigest ?? attempt?.descriptorDigest,
            failureCode: entry.failureCode,
            admittedCaptureID: entry.admittedCaptureID
        )
    }

    private static func fact(_ entry: CaptureQueueEntry) -> CaptureActivityFact {
        switch entry.state {
        case .pending: .queued
        case .staging: .sending
        case .unknownOutcome: .checking
        case .admitted: .processing
        case .failed: .failed
        }
    }

    /// Only the member states that exist before the queue holds the item.
    ///
    /// `published` and `admitted` always have a queue entry that proves what was
    /// actually sent, so reaching them here means the required entry is missing
    /// and the two ledgers disagree. Nothing local can then say whether anything
    /// was sent, so this is neither planned nor waiting: it is an unknown
    /// outcome, which keeps the member's frozen identity for reconciliation,
    /// shows as needing attention, allocates no replacement and is never
    /// resolved, cleared or pruned.
    private static func memberFact(_ member: FolderBulkRunMember) -> CaptureActivityFact {
        switch member.state {
        case .planned: .planned
        case .stopped: .stopped
        case .needsAttention: .failed
        case .published, .admitted: .checking
        }
    }

    private static let formatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        return formatter
    }()

    /// Publishes one state file, owner-only and durable, or changes nothing.
    ///
    /// The bytes and the mode reach the device before the name does, and the
    /// containing directory is flushed after it, so a crash leaves either the
    /// previous state file or this one and never a published name whose contents
    /// were never written. Every step fails closed: the caller only commits its
    /// in-memory records once this has returned.
    private func persist(
        _ values: [String: CaptureActivityRecord],
        nextSequence: UInt64
    ) throws {
        let temporary = directory.appendingPathComponent(
            ".capture-activity-\(UUID().uuidString).tmp"
        )
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
            let data = try encoder.encode(
                CaptureActivityDocument(
                    schema: Self.schema,
                    nextSequence: nextSequence,
                    records: values.values.sorted { $0.sequence < $1.sequence }
                )
            )
            guard data.count <= Self.maximumStateBytes else {
                throw CaptureActivityError.stateUnavailable
            }
            try data.write(to: temporary, options: [.withoutOverwriting])
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: temporary.path
            )
            try Self.sync(temporary)
            if FileManager.default.fileExists(atPath: stateFile.path) {
                _ = try FileManager.default.replaceItemAt(stateFile, withItemAt: temporary)
            } else {
                try FileManager.default.moveItem(at: temporary, to: stateFile)
            }
            // Replacement can carry the replaced file's metadata, so owner-only
            // is reasserted on the published file and then flushed with it.
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: stateFile.path
            )
            try Self.sync(stateFile)
            try Self.sync(directory, isDirectory: true)
        } catch let error as CaptureActivityError {
            try? FileManager.default.removeItem(at: temporary)
            throw error
        } catch {
            try? FileManager.default.removeItem(at: temporary)
            throw CaptureActivityError.stateUnavailable
        }
    }

    /// Flushes one file or directory to the device, never through a symlink.
    private static func sync(_ url: URL, isDirectory: Bool = false) throws {
        var flags = O_RDONLY | O_CLOEXEC | O_NOFOLLOW
        if isDirectory { flags |= O_DIRECTORY }
        let descriptor = Darwin.open(url.path, flags)
        guard descriptor >= 0 else { throw CaptureActivityError.stateUnavailable }
        defer { Darwin.close(descriptor) }
        // F_FULLFSYNC reaches the device; it is unsupported on some filesystems,
        // where fsync is all there is.
        guard Darwin.fcntl(descriptor, F_FULLFSYNC) == 0 || Darwin.fsync(descriptor) == 0 else {
            throw CaptureActivityError.stateUnavailable
        }
    }

    private static func load(
        _ stateFile: URL
    ) throws -> (records: [String: CaptureActivityRecord], nextSequence: UInt64) {
        guard FileManager.default.fileExists(atPath: stateFile.path) else { return ([:], 1) }
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: stateFile.path)
            guard (attributes[.type] as? FileAttributeType) == .typeRegular,
                  let size = attributes[.size] as? NSNumber,
                  size.intValue <= maximumStateBytes,
                  let owner = attributes[.ownerAccountID] as? NSNumber,
                  owner.uint32Value == Darwin.getuid(),
                  let permissions = attributes[.posixPermissions] as? NSNumber,
                  permissions.intValue & 0o077 == 0
            else { throw CaptureActivityError.invalidState }
            let data = try Data(contentsOf: stateFile, options: [.mappedIfSafe])
            let document = try JSONDecoder().decode(CaptureActivityDocument.self, from: data)
            guard document.schema == schema else { throw CaptureActivityError.invalidState }
            var result: [String: CaptureActivityRecord] = [:]
            for record in document.records {
                try validate(record)
                guard result[record.itemID] == nil,
                      record.sequence < document.nextSequence
                else { throw CaptureActivityError.invalidState }
                result[record.itemID] = record
            }
            guard Set(result.values.map(\.sequence)).count == result.count else {
                throw CaptureActivityError.invalidState
            }
            return (result, document.nextSequence)
        } catch let error as CaptureActivityError {
            throw error
        } catch {
            throw CaptureActivityError.invalidState
        }
    }

    private static func validate(_ record: CaptureActivityRecord) throws {
        let identifiers = [record.itemID, record.submissionID, record.operationKey]
            + [record.runID, record.manifestRevision, record.memberID, record.attemptOperationKey,
               record.failureCode, record.admittedCaptureID].compactMap { $0 }
        // A member named by a run carries all three of its grouping facts or
        // none of them; a half-recorded group cannot be projected.
        let grouping = [record.runID, record.manifestRevision, record.memberID]
            .compactMap { $0 }
            .count
        let digests = [record.attemptRequestDigest, record.attemptDescriptorDigest]
            .compactMap { $0 }
        // A resolution time exists for exactly the facts that resolve. An item
        // Core merely accepted for processing is not one of them, so a file
        // claiming it resolved is refused rather than read as a receipt that
        // could then be cleared or aged out.
        guard identifiers.allSatisfy(validIdentifier),
              record.sequence > 0,
              !record.displayName.isEmpty,
              record.displayName.count <= 255,
              !record.displayName.contains("\0"),
              formatter.date(from: record.firstObservedAt) != nil,
              record.fact.isResolved == (record.resolvedAt != nil),
              record.resolvedAt.map({ formatter.date(from: $0) != nil }) ?? true,
              grouping == 0 || grouping == 3,
              digests.allSatisfy(validDigest)
        else { throw CaptureActivityError.invalidState }
    }

    private static func validIdentifier(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= 128
            && value.unicodeScalars.allSatisfy {
                CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
                    .contains($0)
            }
    }

    private static func validDigest(_ value: String) -> Bool {
        value.count == 71
            && value.hasPrefix("sha256:")
            && value.dropFirst(7).allSatisfy { $0.isHexDigit && !$0.isUppercase }
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
                throw CaptureActivityError.stateUnavailable
            }
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o700],
                ofItemAtPath: directory.path
            )
        } catch let error as CaptureActivityError {
            throw error
        } catch {
            throw CaptureActivityError.stateUnavailable
        }
    }
}
