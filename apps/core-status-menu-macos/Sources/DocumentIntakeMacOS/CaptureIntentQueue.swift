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
private func rejectUnknownKeys<Keys: CodingKey & CaseIterable>(
    _ decoder: Decoder,
    _ keys: Keys.Type
) throws {
    let allowed = Set(Keys.allCases.map(\.stringValue))
    let raw = try decoder.container(keyedBy: CaptureQueueAnyKey.self)
    guard Set(raw.allKeys.map(\.stringValue)).isSubset(of: allowed) else {
        throw CaptureQueueError.incompatibleSchema
    }
}

/// Owner-only durable queue for frozen capture intents and their private payload bytes.
/// Mutable scheduling state is separate from each immutable intent; unknown outcomes never receive substitute keys.
public final class CaptureIntentQueue: @unchecked Sendable {
    public static let maximumUnresolvedItems = 100
    public static let maximumUnresolvedPayloadBytes: UInt64 = 512 * 1024 * 1024
    public static let maximumOwnedBytes: UInt64 = 1024 * 1024 * 1024

    private static let schema = "omnivia.macos.capture-queue.v2"
    private static let legacySchema = "omnivia.macos.capture-queue.v1"
    private let lock = NSLock()
    private let root: URL
    private let payloadDirectory: URL
    private let manifest: URL
    private var entries: [String: CaptureQueueEntry]

    public init(root: URL) throws {
        self.root = root
        payloadDirectory = root.appendingPathComponent("payloads", isDirectory: true)
        manifest = root.appendingPathComponent("capture-queue-v1.json", isDirectory: false)
        try Self.prepareDirectory(root)
        try Self.prepareDirectory(payloadDirectory)
        entries = try Self.load(manifest)
        // Admission is durable before payload cleanup. Finish that cleanup after a crash.
        for entry in entries.values where entry.state == .admitted {
            try? FileManager.default.removeItem(at: payloadPath(for: entry.intent))
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
        lock.withLock { entries.values.sorted { $0.intent.itemId < $1.intent.itemId } }
    }

    public func payloadURL(for itemID: String) throws -> URL {
        try lock.withLock {
            guard let entry = entries[itemID] else { throw CaptureQueueError.unknownItem }
            guard Self.unresolved(entry.state) else { throw CaptureQueueError.invalidTransition }
            return payloadDirectory.appendingPathComponent(entry.intent.payloadRef + ".payload")
        }
    }

    @discardableResult
    public func enqueue(intent: CaptureIntentV3, payload: Data) throws -> CaptureQueueEntry {
        try enqueueBatch([(intent: intent, payload: payload)])[0]
    }

    /// Publishes a fully prepared selection as one manifest transition. Either every new item is
    /// visible after restart or none is; exact replays return their existing entries.
    @discardableResult
    public func enqueueBatch(
        _ items: [(intent: CaptureIntentV3, payload: Data)]
    ) throws -> [CaptureQueueEntry] {
        guard !items.isEmpty,
              Set(items.map { $0.intent.itemId }).count == items.count,
              Set(items.map { $0.intent.payloadRef }).count == items.count
        else { throw CaptureQueueError.duplicateItem }
        let prepared = try items.map { item -> (CaptureIntentV3, Data, String, UInt64) in
            try item.intent.validate()
            let canonical = try item.intent.canonicalData()
            let intentDigest = try item.intent.requestDigest()
            guard UInt64(item.payload.count) == item.intent.byteLength,
                  Self.digest(item.payload) == item.intent.localIntegrityDigest
            else { throw CaptureQueueError.payloadMismatch }
            return (item.intent, item.payload, intentDigest, UInt64(canonical.count))
        }
        return try lock.withLock {
            var results: [CaptureQueueEntry] = []
            var newItems: [(CaptureIntentV3, Data, String)] = []
            for (intent, payload, intentDigest, _) in prepared {
                if let existing = entries[intent.itemId] {
                    guard existing.intentDigest == intentDigest,
                          let existingPayload = try? Data(contentsOf: payloadPath(for: intent)),
                          existingPayload == payload
                    else { throw CaptureQueueError.duplicateItem }
                    results.append(existing)
                } else {
                    guard !FileManager.default.fileExists(atPath: payloadPath(for: intent).path) else {
                        throw CaptureQueueError.duplicateItem
                    }
                    let entry = CaptureQueueEntry(
                        intent: intent,
                        intentDigest: intentDigest,
                        state: .pending,
                        attemptGeneration: 0,
                        stageRequestDigest: nil,
                        stageDescriptorDigest: nil,
                        admittedCaptureID: nil,
                        failureCode: nil,
                        attempts: []
                    )
                    results.append(entry)
                    newItems.append((intent, payload, intentDigest))
                }
            }
            let unresolved = entries.values.filter { Self.unresolved($0.state) }
            let unresolvedBytes = unresolved.reduce(UInt64(0)) { $0 + $1.intent.byteLength }
            let ownedBytes = entries.values.reduce(UInt64(0)) { total, entry in
                total + (entry.state == .admitted ? 0 : entry.intent.byteLength)
            }
            let addedBytes = newItems.reduce(UInt64(0)) { $0 + $1.0.byteLength }
            let addedMetadata = prepared.reduce(UInt64(0)) { total, item in
                total + (entries[item.0.itemId] == nil ? item.3 : 0)
            }
            guard unresolved.count + newItems.count <= Self.maximumUnresolvedItems,
                  unresolvedBytes + addedBytes <= Self.maximumUnresolvedPayloadBytes,
                  ownedBytes + addedBytes + addedMetadata <= Self.maximumOwnedBytes
            else { throw CaptureQueueError.capacityExceeded }
            var written: [URL] = []
            do {
                for (intent, payload, _) in newItems {
                    let path = payloadPath(for: intent)
                    try payload.write(to: path, options: [.atomic])
                    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
                    written.append(path)
                }
            } catch {
                for path in written { try? FileManager.default.removeItem(at: path) }
                throw CaptureQueueError.stateUnavailable
            }
            var replacement = entries
            for entry in results where replacement[entry.intent.itemId] == nil {
                replacement[entry.intent.itemId] = entry
            }
            do {
                try persist(replacement)
            } catch {
                for path in written { try? FileManager.default.removeItem(at: path) }
                throw error
            }
            entries = replacement
            return results
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
            guard current.state == .pending else { throw CaptureQueueError.invalidTransition }
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
        try? FileManager.default.removeItem(at: payloadPath(for: terminal.intent))
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
        try lock.withLock {
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
        payloadDirectory.appendingPathComponent(intent.payloadRef + ".payload", isDirectory: false)
    }

    private func persist(_ values: [String: CaptureQueueEntry]) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        do {
            let data = try encoder.encode(
                CaptureQueueDocument(
                    schema: Self.schema,
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
                  storedSchema == schema || storedSchema == legacySchema
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
