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

public struct CaptureQueueEntry: Codable, Equatable, Sendable {
    public let intent: CaptureIntentV3
    public let intentDigest: String
    public let state: CaptureQueueState
    public let attemptGeneration: UInt64
    public let stageRequestDigest: String?
    public let stageDescriptorDigest: String?
    public let admittedCaptureID: String?
    public let failureCode: String?

    fileprivate func replacing(
        state: CaptureQueueState,
        attemptGeneration: UInt64? = nil,
        stageRequestDigest: String? = nil,
        stageDescriptorDigest: String? = nil,
        admittedCaptureID: String? = nil,
        failureCode: String? = nil
    ) -> CaptureQueueEntry {
        CaptureQueueEntry(
            intent: intent,
            intentDigest: intentDigest,
            state: state,
            attemptGeneration: attemptGeneration ?? self.attemptGeneration,
            stageRequestDigest: stageRequestDigest,
            stageDescriptorDigest: stageDescriptorDigest,
            admittedCaptureID: admittedCaptureID,
            failureCode: failureCode
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
        let raw = try decoder.container(keyedBy: CaptureQueueAnyKey.self)
        let allowed = Set(CodingKeys.allCases.map(\.rawValue))
        guard Set(raw.allKeys.map(\.stringValue)).isSubset(of: allowed) else {
            throw CaptureQueueError.incompatibleSchema
        }
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

/// Owner-only durable queue for frozen capture intents and their private payload bytes.
/// Mutable scheduling state is separate from each immutable intent; unknown outcomes never receive substitute keys.
public final class CaptureIntentQueue: @unchecked Sendable {
    public static let maximumUnresolvedItems = 100
    public static let maximumUnresolvedPayloadBytes: UInt64 = 512 * 1024 * 1024
    public static let maximumOwnedBytes: UInt64 = 1024 * 1024 * 1024

    private static let schema = "omnivia.macos.capture-queue.v1"
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
                    stageDescriptorDigest: entry.stageDescriptorDigest
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
        try intent.validate()
        let canonical = try intent.canonicalData()
        let intentDigest = try intent.requestDigest()
        guard UInt64(payload.count) == intent.byteLength,
              Self.digest(payload) == intent.localIntegrityDigest
        else { throw CaptureQueueError.payloadMismatch }
        return try lock.withLock {
            if let existing = entries[intent.itemId] {
                if existing.intentDigest == intentDigest,
                   let existingPayload = try? Data(contentsOf: payloadPath(for: intent)),
                   existingPayload == payload
                {
                    return existing
                }
                throw CaptureQueueError.duplicateItem
            }
            let unresolved = entries.values.filter { Self.unresolved($0.state) }
            let unresolvedBytes = unresolved.reduce(UInt64(0)) { $0 + $1.intent.byteLength }
            let ownedBytes = entries.values.reduce(UInt64(0)) { total, entry in
                total + (entry.state == .admitted ? 0 : entry.intent.byteLength)
            }
            guard unresolved.count < Self.maximumUnresolvedItems,
                  unresolvedBytes + intent.byteLength <= Self.maximumUnresolvedPayloadBytes,
                  ownedBytes + intent.byteLength + UInt64(canonical.count) <= Self.maximumOwnedBytes
            else { throw CaptureQueueError.capacityExceeded }
            let path = payloadPath(for: intent)
            guard !FileManager.default.fileExists(atPath: path.path) else {
                throw CaptureQueueError.duplicateItem
            }
            do {
                try payload.write(to: path, options: [.atomic])
                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
            } catch {
                throw CaptureQueueError.stateUnavailable
            }
            let entry = CaptureQueueEntry(
                intent: intent,
                intentDigest: intentDigest,
                state: .pending,
                attemptGeneration: 0,
                stageRequestDigest: nil,
                stageDescriptorDigest: nil,
                admittedCaptureID: nil,
                failureCode: nil
            )
            var replacement = entries
            replacement[intent.itemId] = entry
            do {
                try persist(replacement)
            } catch {
                try? FileManager.default.removeItem(at: path)
                throw error
            }
            entries = replacement
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
            guard current.state == .pending else { throw CaptureQueueError.invalidTransition }
            return current.replacing(
                state: .staging,
                attemptGeneration: current.attemptGeneration + 1,
                stageRequestDigest: requestDigest,
                stageDescriptorDigest: descriptorDigest
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
                stageDescriptorDigest: current.stageDescriptorDigest
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
            guard current.state == .unknownOutcome,
                  current.attemptGeneration == attemptGeneration,
                  current.stageRequestDigest == requestDigest,
                  current.stageDescriptorDigest == descriptorDigest
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
            return current.replacing(state: .admitted, admittedCaptureID: captureID)
        }
        try? FileManager.default.removeItem(at: payloadPath(for: terminal.intent))
        return terminal
    }

    @discardableResult
    public func markFailed(itemID: String, failureCode: String) throws -> CaptureQueueEntry {
        guard Self.validIdentifier(failureCode) else { throw CaptureQueueError.invalidTransition }
        return try mutate(itemID) { current in
            guard Self.unresolved(current.state) else { throw CaptureQueueError.invalidTransition }
            return current.replacing(state: .failed, failureCode: failureCode)
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
            let temporary = root.appendingPathComponent(".capture-queue-(UUID().uuidString).tmp")
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
                  rawDocument["schema"] as? String == schema
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
