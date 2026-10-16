import CryptoKit
import Darwin
import Foundation

public enum CaptureQueueError: Error, Equatable {
    case capacityExceeded
    case duplicateItem
    case unknownItem
    case invalidTransition
    case payloadMismatch
    case stateUnavailable
    case incompatibleSchema
}

public enum CaptureQueueState: String, Codable, Sendable {
    case pending
    case staging
    case unknownOutcome = "unknown_outcome"
    case admitted
    case failed
}

/// Why a pending intent is held. Both holds follow a refusal that provably
/// happened before anything was sent, so the intent keeps its bytes, its stable
/// key and its history, and nothing is sent or re-keyed while it is held. Only
/// an explicit release returns it to ordinary pending work.
public enum CaptureQueuePause: String, Codable, Equatable, Sendable {
    /// The target no longer accepts this client's credential.
    case needsAccess = "needs_access"
    /// This client and the target could not agree on a protocol version.
    case updateRequired = "update_required"
}

/// What is conclusively known about one externally visible attempt.
/// `active` and `unknown` are open: the attempt may still have had an effect.
public enum CaptureAttemptDisposition: String, Codable, Equatable, Sendable {
    case active
    case unknown
    case knownNoEffect = "known_no_effect"
    case admitted
    case failed
}

/// One stage/admission attempt. Its generation, digests and child operation key are
/// frozen when the attempt starts and are never rewritten; only an open disposition
/// is narrowed, and only by evidence about that exact attempt.
public struct CaptureAttemptRecord: Codable, Equatable, Sendable {
    private static let domain = Data("omnivia.macos.capture-attempt.v1".utf8)

    public let generation: UInt64
    public let operationKey: String
    public let requestDigest: String
    public let descriptorDigest: String
    public let disposition: CaptureAttemptDisposition

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case generation
        case operationKey
        case requestDigest
        case descriptorDigest
        case disposition
    }

    fileprivate init(
        generation: UInt64,
        operationKey: String,
        requestDigest: String,
        descriptorDigest: String,
        disposition: CaptureAttemptDisposition
    ) {
        self.generation = generation
        self.operationKey = operationKey
        self.requestDigest = requestDigest
        self.descriptorDigest = descriptorDigest
        self.disposition = disposition
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        generation = try container.decode(UInt64.self, forKey: .generation)
        operationKey = try container.decode(String.self, forKey: .operationKey)
        requestDigest = try container.decode(String.self, forKey: .requestDigest)
        descriptorDigest = try container.decode(String.self, forKey: .descriptorDigest)
        disposition = try container.decode(CaptureAttemptDisposition.self, forKey: .disposition)
    }

    /// A distinct operation key per attempt, derived only from the stable parent
    /// intent key and the attempt generation. A replacement attempt therefore never
    /// reuses the key of an attempt whose effect is still unknown.
    public static func childOperationKey(parent: String, generation: UInt64) -> String {
        var material = domain
        material.append(0)
        material.append(contentsOf: parent.utf8)
        material.append(0)
        material.append(contentsOf: String(generation).utf8)
        return "capop-" + SHA256.hash(data: material).map { String(format: "%02x", $0) }.joined()
    }

    fileprivate func settled(_ disposition: CaptureAttemptDisposition) -> CaptureAttemptRecord {
        CaptureAttemptRecord(
            generation: generation,
            operationKey: operationKey,
            requestDigest: requestDigest,
            descriptorDigest: descriptorDigest,
            disposition: disposition
        )
    }
}

public struct CaptureQueueEntry: Codable, Equatable, Sendable {
    public let intent: CaptureIntentV3
    public let intentDigest: String
    public let state: CaptureQueueState
    public let attemptGeneration: UInt64
    public let stageRequestDigest: String?
    public let stageDescriptorDigest: String?
    public let admittedCaptureID: String?
    public let failureCode: String?
    /// Set only on a pending item whose last attempt was refused before it was
    /// sent. A held item is not delivered.
    public let pause: CaptureQueuePause?
    /// Every attempt started for this item, oldest first. Append-only.
    public let attempts: [CaptureAttemptRecord]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case intent
        case intentDigest
        case state
        case attemptGeneration
        case stageRequestDigest
        case stageDescriptorDigest
        case admittedCaptureID
        case failureCode
        case pause
        case attempts
    }

    fileprivate init(
        intent: CaptureIntentV3,
        intentDigest: String,
        state: CaptureQueueState,
        attemptGeneration: UInt64,
        stageRequestDigest: String?,
        stageDescriptorDigest: String?,
        admittedCaptureID: String?,
        failureCode: String?,
        pause: CaptureQueuePause?,
        attempts: [CaptureAttemptRecord]
    ) {
        self.intent = intent
        self.intentDigest = intentDigest
        self.state = state
        self.attemptGeneration = attemptGeneration
        self.stageRequestDigest = stageRequestDigest
        self.stageDescriptorDigest = stageDescriptorDigest
        self.admittedCaptureID = admittedCaptureID
        self.failureCode = failureCode
        self.pause = pause
        self.attempts = attempts
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        intent = try container.decode(CaptureIntentV3.self, forKey: .intent)
        intentDigest = try container.decode(String.self, forKey: .intentDigest)
        state = try container.decode(CaptureQueueState.self, forKey: .state)
        attemptGeneration = try container.decode(UInt64.self, forKey: .attemptGeneration)
        stageRequestDigest = try container.decodeIfPresent(String.self, forKey: .stageRequestDigest)
        stageDescriptorDigest = try container.decodeIfPresent(String.self, forKey: .stageDescriptorDigest)
        admittedCaptureID = try container.decodeIfPresent(String.self, forKey: .admittedCaptureID)
        failureCode = try container.decodeIfPresent(String.self, forKey: .failureCode)
        pause = try container.decodeIfPresent(CaptureQueuePause.self, forKey: .pause)
        if let recorded = try container.decodeIfPresent([CaptureAttemptRecord].self, forKey: .attempts) {
            attempts = recorded
        } else if attemptGeneration > 0,
                  let request = stageRequestDigest,
                  let descriptor = stageDescriptorDigest,
                  let disposition = Self.legacyDisposition(state) {
            // A manifest written before attempt history existed. Reconstruct only
            // what its scheduling fields already state; never invent evidence.
            attempts = [
                CaptureAttemptRecord(
                    generation: attemptGeneration,
                    operationKey: CaptureAttemptRecord.childOperationKey(
                        parent: intent.operationKey,
                        generation: attemptGeneration
                    ),
                    requestDigest: request,
                    descriptorDigest: descriptor,
                    disposition: disposition
                ),
            ]
        } else {
            attempts = []
        }
    }

    private static func legacyDisposition(_ state: CaptureQueueState) -> CaptureAttemptDisposition? {
        switch state {
        case .staging: .active
        case .unknownOutcome: .unknown
        // Older terminal and recovered entries dropped their attempt digests, so
        // there is nothing truthful left to reconstruct them from.
        case .pending, .admitted, .failed: nil
        }
    }

    /// Narrows the open attempt, if there is one. A settled attempt is never rewritten.
    fileprivate func settlingLast(_ disposition: CaptureAttemptDisposition) -> [CaptureAttemptRecord] {
        guard let last = attempts.last,
              last.disposition == .active || last.disposition == .unknown
        else { return attempts }
        return Array(attempts.dropLast()) + [last.settled(disposition)]
    }

    fileprivate func replacing(
        state: CaptureQueueState,
        attemptGeneration: UInt64? = nil,
        stageRequestDigest: String? = nil,
        stageDescriptorDigest: String? = nil,
        admittedCaptureID: String? = nil,
        failureCode: String? = nil,
        pause: CaptureQueuePause? = nil,
        attempts: [CaptureAttemptRecord]? = nil
    ) -> CaptureQueueEntry {
        CaptureQueueEntry(
            intent: intent,
            intentDigest: intentDigest,
            state: state,
            attemptGeneration: attemptGeneration ?? self.attemptGeneration,
            stageRequestDigest: stageRequestDigest,
            stageDescriptorDigest: stageDescriptorDigest,
            admittedCaptureID: admittedCaptureID,
            failureCode: failureCode,
            pause: pause,
            attempts: attempts ?? self.attempts
        )
    }
}

private struct CaptureQueueDocument: Codable {
    let schema: String
    let entries: [CaptureQueueEntry]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schema
        case entries
    }

    init(schema: String, entries: [CaptureQueueEntry]) {
        self.schema = schema
        self.entries = entries
    }

    init(from decoder: Decoder) throws {
        try rejectUnknownKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schema = try container.decode(String.self, forKey: .schema)
        entries = try container.decode([CaptureQueueEntry].self, forKey: .entries)
    }
}

private struct CaptureQueueAnyKey: CodingKey {
    let stringValue: String
    let intValue: Int? = nil

    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

/// Refuses any field this build does not define, so a manifest carrying future
/// authority is rejected rather than silently read as a subset.
func rejectUnknownKeys<Keys: CodingKey & CaseIterable>(
    _ decoder: Decoder,
    _ keys: Keys.Type
) throws {
    let allowed = Set(Keys.allCases.map(\.stringValue))
    let raw = try decoder.container(keyedBy: CaptureQueueAnyKey.self)
    guard Set(raw.allKeys.map(\.stringValue)).isSubset(of: allowed) else {
        throw CaptureQueueError.incompatibleSchema
    }
}

/// The closed manifest a candidate directory carries, and the only record a
/// published batch needs to be recovered without the index.
private struct CaptureBatchManifest: Codable {
    static let currentSchema = "omnivia.macos.capture-batch.v1"
    static let filename = "batch.json"

    let schema: String
    let batchId: String
    let batchDigest: String
    let intents: [CaptureIntentV3]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schema
        case batchId
        case batchDigest
        case intents
    }

    init(batchId: String, batchDigest: String, intents: [CaptureIntentV3]) {
        schema = Self.currentSchema
        self.batchId = batchId
        self.batchDigest = batchDigest
        self.intents = intents
    }

    init(from decoder: Decoder) throws {
        try rejectUnknownKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schema = try container.decode(String.self, forKey: .schema)
        batchId = try container.decode(String.self, forKey: .batchId)
        batchDigest = try container.decode(String.self, forKey: .batchDigest)
        intents = try container.decode([CaptureIntentV3].self, forKey: .intents)
    }
}

/// One complete queue batch, prepared on the queue's own filesystem but not yet
/// published: a private directory holding a closed manifest and every immutable
/// payload, already validated byte for byte and fsynced along with its parent.
///
/// Publication is a single rename of this directory, so no payload byte is ever
/// written while the publication gate is held.
public struct CaptureQueueCandidate: Sendable {
    fileprivate let batchId: String
    fileprivate let batchDigest: String
    fileprivate let directory: URL
    fileprivate let intents: [CaptureIntentV3]
    fileprivate let requestedItemIds: [String]
    fileprivate let payloadBytes: UInt64
    fileprivate let reservedBytes: UInt64

    public var itemIds: [String] { requestedItemIds }
}

/// A candidate directory that was renamed into the published namespace. These
/// directories, not the index, are the recovery source of truth.
private struct PublishedBatch {
    let batchId: String
    let batchDigest: String
    let directory: URL
    let intents: [CaptureIntentV3]

    func payload(_ intent: CaptureIntentV3) -> URL {
        directory.appendingPathComponent(intent.payloadRef + ".payload", isDirectory: false)
    }
}

private struct CaptureCandidateReservation {
    let itemIds: Set<String>
    let addedItems: Int
    let addedPayloadBytes: UInt64
    let ownedBytes: UInt64
}

/// Owner-only durable queue for frozen capture intents and their private payload bytes.
/// Mutable scheduling state is separate from each immutable intent; unknown outcomes never receive substitute keys.
///
/// Queue candidate preparation and publication are separate phases.
/// `prepareCandidate` writes and verifies a whole batch outside the publication
/// gate; `publish` takes the gate and does nothing but reconcile, account,
/// rename one directory and save the index. The published directories carry
/// enough to rebuild the index, so a rename whose acknowledgement was lost is
/// recovered at startup rather than lost or duplicated.
public final class CaptureIntentQueue: @unchecked Sendable {
    public static let maximumUnresolvedItems = 100
    public static let maximumUnresolvedPayloadBytes: UInt64 = 512 * 1024 * 1024
    public static let maximumOwnedBytes: UInt64 = 1024 * 1024 * 1024

    private static let schema = "omnivia.macos.capture-queue.v2"
    /// Written only while an item is held, so a build that predates holds
    /// refuses the manifest instead of reading the hold as plain pending work
    /// and sending it.
    private static let holdSchema = "omnivia.macos.capture-queue.v3"
    private static let legacySchema = "omnivia.macos.capture-queue.v1"
    private static let batchDomain = Data("omnivia.macos.capture-batch.v1".utf8)
    /// Shared with the other publication owners; see `PublicationGate`. Enqueue
    /// publication and every delivery claim/stage transition hold it, so a
    /// claim can never observe a batch the publication boundary has not
    /// committed.
    private let gate: PublicationGate
    private let root: URL
    /// Where v1/v2 data kept its payloads. Nothing is written here any more; a
    /// legacy entry still resolves to it.
    private let payloadDirectory: URL
    private let candidateDirectory: URL
    private let publishedDirectory: URL
    private let manifest: URL
    private var entries: [String: CaptureQueueEntry]
    private var batches: [String: PublishedBatch] = [:]
    private var batchDigestByItem: [String: String] = [:]
    private var candidateReservations: [String: CaptureCandidateReservation] = [:]

    /// Test seam: the owned-bytes ceiling, lowered by a test that has to reach
    /// it without writing half a gigabyte. Production uses `maximumOwnedBytes`.
    var ownedBytesLimit: UInt64 = CaptureIntentQueue.maximumOwnedBytes
    /// Test seam after quota reservation has released the gate and before the
    /// candidate directory or any payload is written.
    var afterCandidateReservation: (() -> Void)?
    /// Test seam: runs inside `publish`, after the candidate directory has been
    /// renamed into the published namespace and before the index is saved.
    /// Throwing from it is a lost acknowledgement. Production leaves it nil.
    var afterPublishRename: (() throws -> Void)?

    public init(root: URL, gate: PublicationGate = .processShared) throws {
        self.gate = gate
        self.root = root
        payloadDirectory = root.appendingPathComponent("payloads", isDirectory: true)
        candidateDirectory = root.appendingPathComponent("candidates", isDirectory: true)
        publishedDirectory = root.appendingPathComponent("published", isDirectory: true)
        manifest = root.appendingPathComponent("capture-queue-v1.json", isDirectory: false)
        try Self.prepareDirectory(root)
        try Self.prepareDirectory(payloadDirectory)
        try Self.prepareDirectory(candidateDirectory)
        try Self.prepareDirectory(publishedDirectory)
        entries = try Self.load(manifest)
        // What was published is what the published directories say, not what the
        // index managed to record: a rename whose acknowledgement was lost is
        // adopted here, exactly once, from its own closed manifest.
        let published = try Self.loadPublished(publishedDirectory, entries: entries)
        var recovered = entries
        for batch in published.values {
            batches[batch.batchDigest] = batch
            for intent in batch.intents {
                batchDigestByItem[intent.itemId] = batch.batchDigest
                if recovered[intent.itemId] == nil {
                    recovered[intent.itemId] = try Self.pendingEntry(intent)
                }
            }
        }
        if recovered.count != entries.count {
            entries = recovered
            try persist(entries)
        }
        // The producer that wrote them is gone and nothing published depends on
        // them, so every unpublished candidate is abandoned by definition.
        Self.discardAll(candidateDirectory)
        // Admission is durable before payload cleanup. Finish that cleanup after a crash.
        for entry in entries.values where entry.state == .admitted {
            reclaim(entry.intent)
        }
        // A process died after it started an externally visible attempt. Its outcome is unknown until reconciled.
        let interrupted = entries.filter { $0.value.state == .staging }
        if !interrupted.isEmpty {
            for (key, entry) in interrupted {
                entries[key] = entry.replacing(
                    state: .unknownOutcome,
                    stageRequestDigest: entry.stageRequestDigest,
                    stageDescriptorDigest: entry.stageDescriptorDigest,
                    attempts: entry.settlingLast(.unknown)
                )
            }
            try persist(entries)
        }
    }

    public func allEntries() -> [CaptureQueueEntry] {
        gate.withLock { entries.values.sorted { $0.intent.itemId < $1.intent.itemId } }
    }

    /// Resolves the private bytes of an unresolved item, whether they were
    /// published as a batch directory or were left in the legacy payload
    /// directory by a v1/v2 queue.
    public func payloadURL(for itemID: String) throws -> URL {
        try gate.withLock {
            guard let entry = entries[itemID] else { throw CaptureQueueError.unknownItem }
            guard Self.unresolved(entry.state) else { throw CaptureQueueError.invalidTransition }
            return payloadPath(for: entry.intent)
        }
    }

    @discardableResult
    public func enqueue(intent: CaptureIntentV3, payload: Data) throws -> CaptureQueueEntry {
        try enqueueBatch([(intent: intent, payload: payload)])[0]
    }

    /// Prepares a whole selection as one publishable candidate and publishes it.
    /// Callers that hold the publication gate for their own final authority
    /// checks must instead call `prepareCandidate` before taking the gate and
    /// `publish` inside it, so no payload byte is written under the gate.
    @discardableResult
    public func enqueueBatch(
        _ items: [(intent: CaptureIntentV3, payload: Data)]
    ) throws -> [CaptureQueueEntry] {
        let candidate = try prepareCandidate(items)
        defer { discard(candidate) }
        return try publish(candidate)
    }

    /// Writes one complete, immutable batch into a private candidate directory
    /// on this queue's own filesystem and proves it before returning: exact
    /// membership, regular files only, exact length and digest for every
    /// payload, owner-only permissions, and every file plus the directory
    /// itself fsynced. Takes no lock and publishes nothing.
    public func prepareCandidate(
        _ items: [(intent: CaptureIntentV3, payload: Data)]
    ) throws -> CaptureQueueCandidate {
        guard !items.isEmpty,
              Set(items.map { $0.intent.itemId }).count == items.count,
              Set(items.map { $0.intent.payloadRef }).count == items.count
        else { throw CaptureQueueError.duplicateItem }
        // Bounded here too: nothing should reach the disk that publication is
        // certain to refuse.
        guard items.count <= Self.maximumUnresolvedItems else {
            throw CaptureQueueError.capacityExceeded
        }
        var payloads: [String: Data] = [:]
        var payloadBytes: UInt64 = 0
        for item in items {
            try item.intent.validate()
            _ = try item.intent.canonicalData()
            guard UInt64(item.payload.count) == item.intent.byteLength,
                  Self.digest(item.payload) == item.intent.localIntegrityDigest
            else { throw CaptureQueueError.payloadMismatch }
            payloads[item.intent.payloadRef] = item.payload
            payloadBytes += item.intent.byteLength
        }
        // Canonical order, so the batch's identity does not depend on the order
        // the caller happened to hand its selection over in.
        let intents = items.map(\.intent).sorted { $0.itemId < $1.itemId }
        let batchId = "capbatch-" + UUID().uuidString.lowercased().filter { $0 != "-" }
        let batchDigest = try Self.batchDigest(intents)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let manifestData = try encoder.encode(
            CaptureBatchManifest(batchId: batchId, batchDigest: batchDigest, intents: intents)
        )
        let (reservedBytes, overflow) = payloadBytes.addingReportingOverflow(
            UInt64(manifestData.count)
        )
        guard !overflow else { throw CaptureQueueError.capacityExceeded }

        // Reserve count and bytes before any payload reaches disk. This short
        // gate section creates no payload; it only prevents concurrent
        // preparations from each believing the same remaining quota is free.
        try gate.withLock {
            let itemIds = Set(intents.map(\.itemId))
            guard candidateReservations.values.allSatisfy({ $0.itemIds.isDisjoint(with: itemIds) })
            else { throw CaptureQueueError.duplicateItem }
            var addedItems = 0
            var addedPayloadBytes: UInt64 = 0
            for intent in intents {
                if let existing = entries[intent.itemId] {
                    guard existing.intent == intent else { throw CaptureQueueError.duplicateItem }
                } else {
                    addedItems += 1
                    addedPayloadBytes += intent.byteLength
                }
            }
            let unresolved = entries.values.filter { Self.unresolved($0.state) }
            let unresolvedBytes = unresolved.reduce(UInt64(0)) { $0 + $1.intent.byteLength }
            let ownedBytes = entries.values.reduce(UInt64(0)) { total, entry in
                total + (entry.state == .admitted ? 0 : entry.intent.byteLength)
            }
            let reservedItems = candidateReservations.values.reduce(0) { $0 + $1.addedItems }
            let reservedPayload = candidateReservations.values.reduce(UInt64(0)) {
                $0 + $1.addedPayloadBytes
            }
            let reservedOwned = candidateReservations.values.reduce(UInt64(0)) {
                $0 + $1.ownedBytes
            }
            guard unresolved.count + reservedItems + addedItems <= Self.maximumUnresolvedItems,
                  unresolvedBytes + reservedPayload + addedPayloadBytes
                    <= Self.maximumUnresolvedPayloadBytes,
                  ownedBytes + reservedOwned + reservedBytes <= ownedBytesLimit
            else { throw CaptureQueueError.capacityExceeded }
            candidateReservations[batchId] = CaptureCandidateReservation(
                itemIds: itemIds,
                addedItems: addedItems,
                addedPayloadBytes: addedPayloadBytes,
                ownedBytes: reservedBytes
            )
        }
        afterCandidateReservation?()
        let directory = candidateDirectory.appendingPathComponent(batchId, isDirectory: true)
        do {
            try Self.prepareDirectory(directory)
            for intent in intents {
                let file = directory.appendingPathComponent(
                    intent.payloadRef + ".payload",
                    isDirectory: false
                )
                try payloads[intent.payloadRef]?.write(to: file, options: [.atomic])
                try FileManager.default.setAttributes(
                    [.posixPermissions: 0o600],
                    ofItemAtPath: file.path
                )
                try Self.sync(file)
            }
            let manifestURL = directory.appendingPathComponent(
                CaptureBatchManifest.filename,
                isDirectory: false
            )
            try manifestData.write(to: manifestURL, options: [.atomic])
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: manifestURL.path
            )
            try Self.sync(manifestURL)
            try Self.validateCandidate(
                directory,
                intents: intents,
                manifestBytes: UInt64(manifestData.count)
            )
            try Self.sync(directory, isDirectory: true)
        } catch {
            try? FileManager.default.removeItem(at: directory)
            gate.withLock { _ = candidateReservations.removeValue(forKey: batchId) }
            throw (error as? CaptureQueueError) ?? .stateUnavailable
        }
        return CaptureQueueCandidate(
            batchId: batchId,
            batchDigest: batchDigest,
            directory: directory,
            intents: intents,
            requestedItemIds: items.map { $0.intent.itemId },
            payloadBytes: payloadBytes,
            reservedBytes: reservedBytes
        )
    }

    /// Publishes a prepared candidate under the shared publication gate. Inside
    /// the gate this reconciles against what is already published, accounts the
    /// quotas, renames the complete candidate directory into the opaque
    /// published namespace, fsyncs that parent and only then exposes the
    /// entries. It copies no payload and calls nothing remote.
    ///
    /// An exact replay of the same submission and items reconciles to the batch
    /// already published; any other overlap is refused.
    @discardableResult
    public func publish(_ candidate: CaptureQueueCandidate) throws -> [CaptureQueueEntry] {
        try gate.withLock {
            guard let reservation = candidateReservations[candidate.batchId],
                  reservation.ownedBytes == candidate.reservedBytes,
                  reservation.itemIds == Set(candidate.intents.map(\.itemId))
            else { throw CaptureQueueError.stateUnavailable }
            if let reconciled = try reconcile(candidate) { return reconciled }
            for intent in candidate.intents {
                guard entries[intent.itemId] == nil,
                      batchDigestByItem[intent.itemId] == nil,
                      !FileManager.default.fileExists(atPath: legacyPayloadPath(intent).path)
                else { throw CaptureQueueError.duplicateItem }
            }
            let unresolved = entries.values.filter { Self.unresolved($0.state) }
            let unresolvedBytes = unresolved.reduce(UInt64(0)) { $0 + $1.intent.byteLength }
            let ownedBytes = entries.values.reduce(UInt64(0)) { total, entry in
                total + (entry.state == .admitted ? 0 : entry.intent.byteLength)
            }
            guard unresolved.count + candidate.intents.count <= Self.maximumUnresolvedItems,
                  unresolvedBytes + candidate.payloadBytes <= Self.maximumUnresolvedPayloadBytes,
                  ownedBytes + candidateReservations.values.reduce(UInt64(0), {
                      $0 + $1.ownedBytes
                  }) <= ownedBytesLimit
            else { throw CaptureQueueError.capacityExceeded }
            let destination = publishedDirectory.appendingPathComponent(
                candidate.batchId,
                isDirectory: true
            )
            guard !FileManager.default.fileExists(atPath: destination.path),
                  Darwin.rename(candidate.directory.path, destination.path) == 0
            else { throw CaptureQueueError.stateUnavailable }
            try Self.sync(publishedDirectory, isDirectory: true)
            let batch = PublishedBatch(
                batchId: candidate.batchId,
                batchDigest: candidate.batchDigest,
                directory: destination,
                intents: candidate.intents
            )
            // The rename is the publication. Record it before anything that can
            // still fail, so a lost acknowledgement cannot publish it twice.
            batches[batch.batchDigest] = batch
            for intent in batch.intents { batchDigestByItem[intent.itemId] = batch.batchDigest }
            candidateReservations.removeValue(forKey: candidate.batchId)
            try afterPublishRename?()
            var replacement = entries
            for intent in candidate.intents {
                replacement[intent.itemId] = try Self.pendingEntry(intent)
            }
            try persist(replacement)
            entries = replacement
            return try candidate.requestedItemIds.map { itemID in
                guard let entry = replacement[itemID] else {
                    throw CaptureQueueError.stateUnavailable
                }
                return entry
            }
        }
    }

    /// Removes an unpublished candidate. A candidate whose directory was already
    /// renamed into the published namespace no longer exists here, so a discard
    /// after a successful publication does nothing.
    public func discard(_ candidate: CaptureQueueCandidate) {
        try? FileManager.default.removeItem(at: candidate.directory)
        gate.withLock { _ = candidateReservations.removeValue(forKey: candidate.batchId) }
    }

    /// The published batch this candidate is an exact replay of, with its
    /// entries exposed. Nil when there is nothing to reconcile against.
    private func reconcile(_ candidate: CaptureQueueCandidate) throws -> [CaptureQueueEntry]? {
        let known = candidate.intents.compactMap { entries[$0.itemId] }
        guard known.count == candidate.intents.count || batches[candidate.batchDigest] != nil else {
            return nil
        }
        var replacement = entries
        for intent in candidate.intents where replacement[intent.itemId] == nil {
            replacement[intent.itemId] = try Self.pendingEntry(intent)
        }
        for intent in candidate.intents {
            guard let entry = replacement[intent.itemId], entry.intent == intent else {
                throw CaptureQueueError.duplicateItem
            }
            // A legacy item still has to be the exact bytes it was enqueued
            // with; a published item is bound by the batch digest already.
            if batchDigestByItem[intent.itemId] == nil {
                guard let bytes = try? Data(contentsOf: legacyPayloadPath(intent)),
                      Self.digest(bytes) == intent.localIntegrityDigest
                else { throw CaptureQueueError.duplicateItem }
            } else if batchDigestByItem[intent.itemId] != candidate.batchDigest {
                throw CaptureQueueError.duplicateItem
            }
        }
        if replacement.count != entries.count {
            try persist(replacement)
            entries = replacement
        }
        return try candidate.requestedItemIds.map { itemID in
            guard let entry = entries[itemID] else {
                throw CaptureQueueError.stateUnavailable
            }
            return entry
        }
    }

    @discardableResult
    public func beginStage(
        itemID: String,
        requestDigest: String,
        descriptorDigest: String
    ) throws -> CaptureQueueEntry {
        guard Self.validDigest(requestDigest), Self.validDigest(descriptorDigest) else {
            throw CaptureQueueError.invalidTransition
        }
        return try mutate(itemID) { current in
            // Only a pending item may start an attempt, so an attempt whose effect is
            // still unknown can never be superseded by a replacement.
            // A held intent starts no attempt until its hold is released.
            guard current.state == .pending, current.pause == nil else {
                throw CaptureQueueError.invalidTransition
            }
            let generation = current.attemptGeneration + 1
            let attempt = CaptureAttemptRecord(
                generation: generation,
                operationKey: CaptureAttemptRecord.childOperationKey(
                    parent: current.intent.operationKey,
                    generation: generation
                ),
                requestDigest: requestDigest,
                descriptorDigest: descriptorDigest,
                disposition: .active
            )
            return current.replacing(
                state: .staging,
                attemptGeneration: generation,
                stageRequestDigest: requestDigest,
                stageDescriptorDigest: descriptorDigest,
                attempts: current.attempts + [attempt]
            )
        }
    }

    @discardableResult
    public func markUnknownOutcome(itemID: String, attemptGeneration: UInt64) throws -> CaptureQueueEntry {
        try mutate(itemID) { current in
            guard current.state == .staging,
                  current.attemptGeneration == attemptGeneration,
                  current.stageRequestDigest != nil,
                  current.stageDescriptorDigest != nil
            else { throw CaptureQueueError.invalidTransition }
            return current.replacing(
                state: .unknownOutcome,
                stageRequestDigest: current.stageRequestDigest,
                stageDescriptorDigest: current.stageDescriptorDigest,
                attempts: current.settlingLast(.unknown)
            )
        }
    }

    @discardableResult
    public func recoverKnownNoEffect(
        itemID: String,
        attemptGeneration: UInt64,
        requestDigest: String,
        descriptorDigest: String
    ) throws -> CaptureQueueEntry {
        try mutate(itemID) { current in
            // Exact evidence about this attempt only. Anything else leaves the attempt
            // unknown, and a still-unknown attempt never returns to pending.
            guard current.state == .unknownOutcome,
                  current.attemptGeneration == attemptGeneration,
                  current.stageRequestDigest == requestDigest,
                  current.stageDescriptorDigest == descriptorDigest,
                  let open = current.attempts.last,
                  open.disposition == .unknown,
                  open.generation == attemptGeneration,
                  open.requestDigest == requestDigest,
                  open.descriptorDigest == descriptorDigest
            else { throw CaptureQueueError.invalidTransition }
            return current.replacing(state: .pending, attempts: current.settlingLast(.knownNoEffect))
        }
    }

    /// Holds an attempt that was refused before anything was sent: the exact
    /// attempt is settled as known-no-effect and the intent returns to pending,
    /// in one durable step, with a pause that stops every later pass from
    /// sending it. No replacement attempt is started, so no key is allocated.
    @discardableResult
    public func hold(
        itemID: String,
        attemptGeneration: UInt64,
        requestDigest: String,
        descriptorDigest: String,
        pause: CaptureQueuePause
    ) throws -> CaptureQueueEntry {
        try mutate(itemID) { current in
            guard current.state == .staging,
                  current.attemptGeneration == attemptGeneration,
                  current.stageRequestDigest == requestDigest,
                  current.stageDescriptorDigest == descriptorDigest,
                  let open = current.attempts.last,
                  open.disposition == .active,
                  open.generation == attemptGeneration
            else { throw CaptureQueueError.invalidTransition }
            return current.replacing(
                state: .pending,
                pause: pause,
                attempts: current.settlingLast(.knownNoEffect)
            )
        }
    }

    /// Returns a held intent to ordinary pending work. Only the owner of the
    /// condition that caused the hold calls this; the next pass then starts a
    /// fresh attempt under the next generation's key, exactly as it would after
    /// any other known-no-effect attempt.
    @discardableResult
    public func releaseHold(itemID: String, pause: CaptureQueuePause) throws -> CaptureQueueEntry {
        try mutate(itemID) { current in
            guard current.state == .pending, current.pause == pause
            else { throw CaptureQueueError.invalidTransition }
            return current.replacing(state: .pending)
        }
    }

    @discardableResult
    public func markAdmitted(
        itemID: String,
        attemptGeneration: UInt64,
        captureID: String
    ) throws -> CaptureQueueEntry {
        guard Self.validCaptureID(captureID) else { throw CaptureQueueError.invalidTransition }
        let terminal = try mutate(itemID) { current in
            guard (current.state == .staging || current.state == .unknownOutcome),
                  current.attemptGeneration == attemptGeneration
            else { throw CaptureQueueError.invalidTransition }
            return current.replacing(
                state: .admitted,
                admittedCaptureID: captureID,
                attempts: current.settlingLast(.admitted)
            )
        }
        gate.withLock { reclaim(terminal.intent) }
        return terminal
    }

    @discardableResult
    public func markFailed(itemID: String, failureCode: String) throws -> CaptureQueueEntry {
        guard Self.validIdentifier(failureCode) else { throw CaptureQueueError.invalidTransition }
        return try mutate(itemID) { current in
            guard Self.unresolved(current.state) else { throw CaptureQueueError.invalidTransition }
            return current.replacing(
                state: .failed,
                failureCode: failureCode,
                attempts: current.settlingLast(.failed)
            )
        }
    }

    private func mutate(
        _ itemID: String,
        change: (CaptureQueueEntry) throws -> CaptureQueueEntry
    ) throws -> CaptureQueueEntry {
        try gate.withLock {
            guard let current = entries[itemID] else { throw CaptureQueueError.unknownItem }
            let changed = try change(current)
            var replacement = entries
            replacement[itemID] = changed
            try persist(replacement)
            entries = replacement
            return changed
        }
    }

    private func payloadPath(for intent: CaptureIntentV3) -> URL {
        guard let digest = batchDigestByItem[intent.itemId], let batch = batches[digest] else {
            return legacyPayloadPath(intent)
        }
        return batch.payload(intent)
    }

    private func legacyPayloadPath(_ intent: CaptureIntentV3) -> URL {
        payloadDirectory.appendingPathComponent(intent.payloadRef + ".payload", isDirectory: false)
    }

    /// Drops private bytes only after canonical admission is durable. Definite
    /// local failure retains the last recovery copy; it is not proof that Core
    /// owns the source. A batch directory is removed only when every member was
    /// admitted and therefore every payload is safely reclaimable.
    private func reclaim(_ intent: CaptureIntentV3) {
        guard let digest = batchDigestByItem[intent.itemId], let batch = batches[digest] else {
            try? FileManager.default.removeItem(at: legacyPayloadPath(intent))
            return
        }
        try? FileManager.default.removeItem(at: batch.payload(intent))
        guard batch.intents.allSatisfy({ entries[$0.itemId]?.state == .admitted })
        else { return }
        try? FileManager.default.removeItem(at: batch.directory)
        for item in batch.intents { batchDigestByItem.removeValue(forKey: item.itemId) }
        batches.removeValue(forKey: digest)
    }

    private func persist(_ values: [String: CaptureQueueEntry]) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        do {
            let data = try encoder.encode(
                CaptureQueueDocument(
                    schema: values.values.contains { $0.pause != nil } ? Self.holdSchema : Self.schema,
                    entries: values.values.sorted { $0.intent.itemId < $1.intent.itemId }
                )
            )
            guard data.count <= 16 * 1024 * 1024 else { throw CaptureQueueError.stateUnavailable }
            let temporary = root.appendingPathComponent(".capture-queue-\(UUID().uuidString).tmp")
            try data.write(to: temporary, options: [.atomic])
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: temporary.path)
            if FileManager.default.fileExists(atPath: manifest.path) {
                _ = try FileManager.default.replaceItemAt(manifest, withItemAt: temporary)
            } else {
                try FileManager.default.moveItem(at: temporary, to: manifest)
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: manifest.path)
            let descriptor = Darwin.open(root.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
            if descriptor >= 0 {
                _ = Darwin.fsync(descriptor)
                Darwin.close(descriptor)
            }
        } catch let error as CaptureQueueError {
            throw error
        } catch {
            throw CaptureQueueError.stateUnavailable
        }
    }

    private static func load(_ manifest: URL) throws -> [String: CaptureQueueEntry] {
        guard FileManager.default.fileExists(atPath: manifest.path) else { return [:] }
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: manifest.path)
            guard (attributes[.type] as? FileAttributeType) == .typeRegular,
                  let size = attributes[.size] as? NSNumber,
                  size.uint64Value <= 16 * 1024 * 1024,
                  let mode = attributes[.posixPermissions] as? NSNumber,
                  mode.intValue & 0o077 == 0
            else { throw CaptureQueueError.stateUnavailable }
            let data = try Data(contentsOf: manifest, options: [.mappedIfSafe])
            let raw = try JSONSerialization.jsonObject(with: data)
            guard let rawDocument = raw as? [String: Any],
                  let storedSchema = rawDocument["schema"] as? String,
                  storedSchema == schema || storedSchema == holdSchema || storedSchema == legacySchema
            else { throw CaptureQueueError.incompatibleSchema }
            let document = try JSONDecoder().decode(CaptureQueueDocument.self, from: data)
            var result: [String: CaptureQueueEntry] = [:]
            for entry in document.entries {
                try entry.intent.validate()
                guard entry.intentDigest == (try entry.intent.requestDigest()),
                      result[entry.intent.itemId] == nil,
                      valid(entry)
                else { throw CaptureQueueError.stateUnavailable }
                result[entry.intent.itemId] = entry
            }
            return result
        } catch let error as CaptureQueueError {
            throw error
        } catch {
            throw CaptureQueueError.stateUnavailable
        }
    }

    private static func valid(_ entry: CaptureQueueEntry) -> Bool {
        guard validHistory(entry) else { return false }
        // A hold is a property of pending work that was refused before it was
        // sent, so it needs the settled attempt that was refused.
        if entry.pause != nil {
            guard entry.state == .pending,
                  entry.attempts.last?.disposition == .knownNoEffect
            else { return false }
        }
        switch entry.state {
        case .pending:
            return entry.stageRequestDigest == nil
                && entry.stageDescriptorDigest == nil
                && entry.admittedCaptureID == nil
                && entry.failureCode == nil
        case .staging, .unknownOutcome:
            return entry.attemptGeneration > 0
                && entry.stageRequestDigest.map(validDigest) == true
                && entry.stageDescriptorDigest.map(validDigest) == true
                && entry.admittedCaptureID == nil
                && entry.failureCode == nil
        case .admitted:
            return entry.admittedCaptureID.map(validCaptureID) == true && entry.failureCode == nil
        case .failed:
            return entry.failureCode.map(validIdentifier) == true && entry.admittedCaptureID == nil
        }
    }

    /// Attempt history is ordered, keyed to this intent, and supersedes an earlier
    /// attempt only where that attempt was conclusively known to have had no effect.
    private static func validHistory(_ entry: CaptureQueueEntry) -> Bool {
        var previous: UInt64 = 0
        for attempt in entry.attempts {
            guard attempt.generation > previous,
                  attempt.generation <= entry.attemptGeneration,
                  attempt.operationKey == CaptureAttemptRecord.childOperationKey(
                      parent: entry.intent.operationKey,
                      generation: attempt.generation
                  ),
                  validDigest(attempt.requestDigest),
                  validDigest(attempt.descriptorDigest)
            else { return false }
            previous = attempt.generation
        }
        guard let last = entry.attempts.last else { return true }
        guard entry.attempts.dropLast().allSatisfy({ $0.disposition == .knownNoEffect }),
              last.generation == entry.attemptGeneration
        else { return false }
        switch entry.state {
        case .pending:
            return last.disposition == .knownNoEffect
        case .staging, .unknownOutcome:
            return last.disposition == (entry.state == .staging ? .active : .unknown)
                && entry.stageRequestDigest == last.requestDigest
                && entry.stageDescriptorDigest == last.descriptorDigest
        case .admitted:
            return last.disposition == .admitted
        case .failed:
            return last.disposition == .failed || last.disposition == .knownNoEffect
        }
    }

    /// Every published batch, keyed by batch digest. Throws rather than skipping:
    /// a published directory that is not exactly its own manifest is authority
    /// this build cannot account for, so the queue refuses to open.
    private static func loadPublished(
        _ directory: URL,
        entries: [String: CaptureQueueEntry]
    ) throws -> [String: PublishedBatch] {
        guard let names = try? FileManager.default.contentsOfDirectory(atPath: directory.path) else {
            throw CaptureQueueError.stateUnavailable
        }
        var result: [String: PublishedBatch] = [:]
        var owners: Set<String> = []
        for name in names.sorted() {
            let batch = try loadBatch(
                directory.appendingPathComponent(name, isDirectory: true),
                entries: entries
            )
            guard result[batch.batchDigest] == nil else { throw CaptureQueueError.stateUnavailable }
            for intent in batch.intents {
                guard owners.insert(intent.itemId).inserted else {
                    throw CaptureQueueError.stateUnavailable
                }
            }
            result[batch.batchDigest] = batch
        }
        return result
    }

    private static func loadBatch(
        _ directory: URL,
        entries: [String: CaptureQueueEntry]
    ) throws -> PublishedBatch {
        guard let directoryFacts = facts(directory),
              directoryFacts.isDirectory,
              directoryFacts.mode & 0o077 == 0
        else { throw CaptureQueueError.stateUnavailable }
        let manifestURL = directory.appendingPathComponent(
            CaptureBatchManifest.filename,
            isDirectory: false
        )
        guard let manifestFacts = facts(manifestURL),
              manifestFacts.isRegular,
              manifestFacts.size <= 16 * 1024 * 1024,
              manifestFacts.mode & 0o077 == 0
        else { throw CaptureQueueError.stateUnavailable }
        let manifest = try decodeManifest(manifestURL)
        guard validBatchID(manifest.batchId),
              manifest.batchId == directory.lastPathComponent,
              !manifest.intents.isEmpty,
              manifest.intents.count <= maximumUnresolvedItems,
              zip(manifest.intents, manifest.intents.dropFirst()).allSatisfy({ $0.itemId < $1.itemId }),
              Set(manifest.intents.map(\.payloadRef)).count == manifest.intents.count,
              try batchDigest(manifest.intents) == manifest.batchDigest
        else { throw CaptureQueueError.stateUnavailable }
        var allowed: Set<String> = [CaptureBatchManifest.filename]
        var required: Set<String> = []
        for intent in manifest.intents {
            try intent.validate()
            let name = intent.payloadRef + ".payload"
            allowed.insert(name)
            // A terminal item's bytes are dropped after its outcome is durable,
            // so only an item that can still be delivered must still be here.
            if entries[intent.itemId].map({ unresolved($0.state) }) ?? true { required.insert(name) }
        }
        let contents = Set((try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? [])
        guard contents.isSubset(of: allowed), required.isSubset(of: contents) else {
            throw CaptureQueueError.stateUnavailable
        }
        for intent in manifest.intents where contents.contains(intent.payloadRef + ".payload") {
            let payload = directory.appendingPathComponent(
                intent.payloadRef + ".payload",
                isDirectory: false
            )
            guard let payloadFacts = facts(payload),
                  payloadFacts.isRegular,
                  payloadFacts.size == intent.byteLength,
                  payloadFacts.mode & 0o077 == 0
            else { throw CaptureQueueError.stateUnavailable }
            // Published bytes are authority-bearing recovery material. Hash
            // every surviving payload at startup, including indexed batches;
            // waiting until delivery would let a corrupt queue open as healthy.
            guard try digestOfFile(payload, length: intent.byteLength)
                == intent.localIntegrityDigest
            else { throw CaptureQueueError.stateUnavailable }
        }
        return PublishedBatch(
            batchId: manifest.batchId,
            batchDigest: manifest.batchDigest,
            directory: directory,
            intents: manifest.intents
        )
    }

    /// Reads the schema before anything else, so a manifest carrying a newer
    /// schema is refused with its bytes untouched rather than read as a subset.
    private static func decodeManifest(_ url: URL) throws -> CaptureBatchManifest {
        do {
            let data = try Data(contentsOf: url, options: [.mappedIfSafe])
            guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let schema = object["schema"] as? String
            else { throw CaptureQueueError.stateUnavailable }
            guard schema == CaptureBatchManifest.currentSchema else {
                throw CaptureQueueError.incompatibleSchema
            }
            return try JSONDecoder().decode(CaptureBatchManifest.self, from: data)
        } catch let error as CaptureQueueError {
            throw error
        } catch {
            throw CaptureQueueError.stateUnavailable
        }
    }

    /// Proves a candidate directory holds exactly this batch and nothing else
    /// before it is eligible for publication.
    private static func validateCandidate(
        _ directory: URL,
        intents: [CaptureIntentV3],
        manifestBytes: UInt64
    ) throws {
        guard let contents = try? FileManager.default.contentsOfDirectory(atPath: directory.path)
        else { throw CaptureQueueError.stateUnavailable }
        var expected: Set<String> = [CaptureBatchManifest.filename]
        for intent in intents { expected.insert(intent.payloadRef + ".payload") }
        guard Set(contents) == expected, contents.count == expected.count else {
            throw CaptureQueueError.stateUnavailable
        }
        guard let manifestFacts = facts(
            directory.appendingPathComponent(CaptureBatchManifest.filename, isDirectory: false)
        ),
            manifestFacts.isRegular,
            manifestFacts.size == manifestBytes,
            manifestFacts.mode & 0o077 == 0
        else { throw CaptureQueueError.stateUnavailable }
        for intent in intents {
            let payload = directory.appendingPathComponent(
                intent.payloadRef + ".payload",
                isDirectory: false
            )
            guard let payloadFacts = facts(payload),
                  payloadFacts.isRegular,
                  payloadFacts.size == intent.byteLength,
                  payloadFacts.mode & 0o077 == 0,
                  try digestOfFile(payload, length: intent.byteLength) == intent.localIntegrityDigest
            else { throw CaptureQueueError.stateUnavailable }
        }
    }

    /// Identity of a batch: its exact item set in canonical order. An exact
    /// replay of the same submission and items therefore reconciles to the batch
    /// already published, and anything else is a different batch.
    private static func batchDigest(_ intents: [CaptureIntentV3]) throws -> String {
        var material = batchDomain
        for intent in intents {
            material.append(0)
            material.append(contentsOf: try intent.requestDigest().utf8)
        }
        return "sha256:" + SHA256.hash(data: material).map { String(format: "%02x", $0) }.joined()
    }

    private static func pendingEntry(_ intent: CaptureIntentV3) throws -> CaptureQueueEntry {
        CaptureQueueEntry(
            intent: intent,
            intentDigest: try intent.requestDigest(),
            state: .pending,
            attemptGeneration: 0,
            stageRequestDigest: nil,
            stageDescriptorDigest: nil,
            admittedCaptureID: nil,
            failureCode: nil,
            pause: nil,
            attempts: []
        )
    }

    /// Removes every child of the candidate directory without following links.
    private static func discardAll(_ directory: URL) {
        guard let directoryFacts = facts(directory), directoryFacts.isDirectory,
              let names = try? FileManager.default.contentsOfDirectory(atPath: directory.path)
        else { return }
        for name in names {
            try? FileManager.default.removeItem(at: directory.appendingPathComponent(name))
        }
    }

    private static func facts(
        _ url: URL
    ) -> (isRegular: Bool, isDirectory: Bool, size: UInt64, mode: mode_t)? {
        var status = stat()
        guard Darwin.lstat(url.path, &status) == 0 else { return nil }
        return (
            status.st_mode & S_IFMT == S_IFREG,
            status.st_mode & S_IFMT == S_IFDIR,
            UInt64(max(0, status.st_size)),
            status.st_mode
        )
    }

    /// Hashes a file that must be exactly `length` bytes, reading no further, so
    /// a file that grew after its length was checked is not streamed unbounded.
    private static func digestOfFile(_ url: URL, length: UInt64) throws -> String {
        guard let input = try? FileHandle(forReadingFrom: url) else {
            throw CaptureQueueError.stateUnavailable
        }
        defer { try? input.close() }
        var hasher = SHA256()
        var read: UInt64 = 0
        while read <= length {
            let wanted = Int(min(UInt64(1024 * 1024), length - read + 1))
            guard let chunk = try? input.read(upToCount: wanted), !chunk.isEmpty else { break }
            read += UInt64(chunk.count)
            guard read <= length else { throw CaptureQueueError.stateUnavailable }
            hasher.update(data: chunk)
        }
        guard read == length else { throw CaptureQueueError.stateUnavailable }
        return "sha256:" + hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    /// Flushes one file or directory to the device, never through a symlink.
    private static func sync(_ url: URL, isDirectory: Bool = false) throws {
        var flags = O_RDONLY | O_CLOEXEC | O_NOFOLLOW
        if isDirectory { flags |= O_DIRECTORY }
        let descriptor = Darwin.open(url.path, flags)
        guard descriptor >= 0 else { throw CaptureQueueError.stateUnavailable }
        defer { Darwin.close(descriptor) }
        // F_FULLFSYNC reaches the device; it is unsupported on some filesystems,
        // where fsync is all there is.
        guard Darwin.fcntl(descriptor, F_FULLFSYNC) == 0 || Darwin.fsync(descriptor) == 0 else {
            throw CaptureQueueError.stateUnavailable
        }
    }

    private static func validBatchID(_ value: String) -> Bool {
        value.count == 41
            && value.hasPrefix("capbatch-")
            && value.dropFirst(9).allSatisfy { $0.isHexDigit && !$0.isUppercase }
    }

    private static func prepareDirectory(_ url: URL) throws {
        do {
            try FileManager.default.createDirectory(
                at: url,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let values = try url.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
            guard values.isDirectory == true, values.isSymbolicLink != true else {
                throw CaptureQueueError.stateUnavailable
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
        } catch {
            throw CaptureQueueError.stateUnavailable
        }
    }

    private static func unresolved(_ state: CaptureQueueState) -> Bool {
        state == .pending || state == .staging || state == .unknownOutcome
    }

    private static func digest(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private static func validIdentifier(_ value: String) -> Bool {
        !value.isEmpty
            && value.utf8.count <= 128
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

    private static func validCaptureID(_ value: String) -> Bool {
        value.count == 69
            && value.hasPrefix("dcap-")
            && value.dropFirst(5).allSatisfy { $0.isHexDigit && !$0.isUppercase }
    }
}
