import Darwin
import Foundation

public struct FileSubscription: Codable, Equatable, Sendable {
    public let subscriptionID: String
    public let accessRef: String
    public let linkedDocumentID: String
    public let requestedSourceIdentity: String
    public let displayName: String
    public let contentKind: CaptureContentKind
    /// The generation of the route this watch reads through. A Locate moves it, once Core has
    /// confirmed the generation the route reserved.
    public var bindingGeneration: UInt64
    /// The queued capture this watch's baseline is. Usually the item its own
    /// submission froze; when the canonical source already held an initial
    /// capture of exactly those bytes, the capture that already serves it.
    public var baselineItemID: String
    public let targetRef: String
    public let workspaceRef: String
    public let targetBindingRevision: String
    public let callerContextBinding: CallerContextBinding
    public let configGeneration: UInt64
    public let consentGeneration: UInt64
    public let consentRecordedAt: String
    public var state: FolderSubscriptionState
    public var currentSuccessfulCaptureID: String?
    public var lastObservedDigest: String?
    public var lastObservedByteLength: UInt64?
    public var lastObservedAt: String?
    public var lastQueuedItemID: String?
    public var dirtyEpoch: UInt64
    public var needsReconciliation: Bool
    public var lastCheckedAt: String?
    /// The latest outcome of a refresh that did not succeed, kept beside the successful capture
    /// rather than over it. Absent for a watch whose latest check or refresh stood.
    public var attention: FileAttention? = nil
}

/// A refresh outcome that is neither "added" nor "up to date" (design §12, §13.2, §13.5).
///
/// It is a record of its own: recording it changes nothing about
/// `currentSuccessfulCaptureID`, and a later check that stands, or a refresh Core admits,
/// replaces it. Persisted with the watch, so it survives a restart as the same fact.
public struct FileAttention: Codable, Equatable, Sendable {
    public enum Kind: String, Codable, Equatable, Sendable {
        /// A refresh was published and Core refused it. `code` is Core's failure code.
        case refreshFailed = "refresh_failed"
        /// The bytes did not change, but Core's copy cannot stand behind "up to date".
        /// `code` is a `CurrentCaptureShortfall` raw value.
        case copyNotCurrent = "copy_not_current"
    }

    public let kind: Kind
    public let code: String
    public let recordedAt: String
    /// The queue item of a failed refresh, which is its own retrievable record.
    public let itemID: String?
    /// The successful capture this outcome left in place.
    public let priorCaptureID: String?

    public init(
        kind: Kind,
        code: String,
        recordedAt: String,
        itemID: String? = nil,
        priorCaptureID: String? = nil
    ) {
        self.kind = kind
        self.code = code
        self.recordedAt = recordedAt
        self.itemID = itemID
        self.priorCaptureID = priorCaptureID
    }
}

public enum FileSubscriptionStoreError: Error, Equatable {
    case duplicateSubscription
    case unknownSubscription
    case invalidState
    case stateUnavailable
}

private struct FileSubscriptionDocument: Codable {
    let schema: String
    let subscriptions: [FileSubscription]
}

public final class FileSubscriptionStore: @unchecked Sendable {
    private static let schema = "omnivia.file-subscriptions.v1"
    /// Shared with the other publication owners; see `PublicationGate`.
    private let gate: PublicationGate
    private let directory: URL
    private let stateFile: URL
    private var values: [String: FileSubscription]

    public init(stateDirectory: URL, gate: PublicationGate = .processShared) throws {
        self.gate = gate
        directory = stateDirectory
        stateFile = stateDirectory.appendingPathComponent("file-subscriptions-v1.json")
        try Self.prepareDirectory(stateDirectory)
        values = try Self.load(stateFile)
    }

    public func all() -> [FileSubscription] {
        gate.withLock { values.values.sorted { $0.subscriptionID < $1.subscriptionID } }
    }

    @discardableResult
    public func register(_ value: FileSubscription) throws -> FileSubscription {
        try gate.withLock {
            guard values[value.subscriptionID] == nil else {
                throw FileSubscriptionStoreError.duplicateSubscription
            }
            try Self.validate(value)
            var replacement = values
            replacement[value.subscriptionID] = value
            try persist(replacement)
            values = replacement
            return value
        }
    }

    @discardableResult
    public func update(
        id: String,
        _ change: (inout FileSubscription) throws -> Void
    ) throws -> FileSubscription {
        try gate.withLock {
            guard var value = values[id] else { throw FileSubscriptionStoreError.unknownSubscription }
            try change(&value)
            try Self.validate(value)
            var replacement = values
            replacement[id] = value
            try persist(replacement)
            values = replacement
            return value
        }
    }

    private func persist(_ values: [String: FileSubscription]) throws {
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
            let data = try encoder.encode(
                FileSubscriptionDocument(
                    schema: Self.schema,
                    subscriptions: values.values.sorted { $0.subscriptionID < $1.subscriptionID }
                )
            )
            guard data.count <= 4 * 1024 * 1024 else { throw FileSubscriptionStoreError.stateUnavailable }
            let temporary = directory.appendingPathComponent(".file-subscriptions-\(UUID().uuidString).tmp")
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
        } catch let error as FileSubscriptionStoreError {
            throw error
        } catch {
            throw FileSubscriptionStoreError.stateUnavailable
        }
    }

    private static func load(_ stateFile: URL) throws -> [String: FileSubscription] {
        guard FileManager.default.fileExists(atPath: stateFile.path) else { return [:] }
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: stateFile.path)
            guard (attributes[.type] as? FileAttributeType) == .typeRegular,
                  let size = attributes[.size] as? NSNumber,
                  size.uint64Value <= 4 * 1024 * 1024,
                  let owner = attributes[.ownerAccountID] as? NSNumber,
                  owner.uint32Value == Darwin.getuid(),
                  let permissions = attributes[.posixPermissions] as? NSNumber,
                  permissions.intValue & 0o077 == 0
            else { throw FileSubscriptionStoreError.invalidState }
            let document = try JSONDecoder().decode(
                FileSubscriptionDocument.self,
                from: Data(contentsOf: stateFile, options: [.mappedIfSafe])
            )
            guard document.schema == schema else { throw FileSubscriptionStoreError.invalidState }
            var result: [String: FileSubscription] = [:]
            for value in document.subscriptions {
                try validate(value)
                guard result[value.subscriptionID] == nil else {
                    throw FileSubscriptionStoreError.invalidState
                }
                result[value.subscriptionID] = value
            }
            return result
        } catch let error as FileSubscriptionStoreError {
            throw error
        } catch {
            throw FileSubscriptionStoreError.invalidState
        }
    }

    private static func validate(_ value: FileSubscription) throws {
        let identifiers = [
            value.subscriptionID, value.accessRef, value.linkedDocumentID,
            value.requestedSourceIdentity, value.baselineItemID, value.targetRef,
            value.workspaceRef, value.targetBindingRevision,
        ]
        guard identifiers.allSatisfy(validIdentifier),
              value.bindingGeneration > 0,
              value.configGeneration > 0,
              value.consentGeneration > 0,
              value.dirtyEpoch > 0,
              !value.displayName.isEmpty,
              value.displayName.count <= 255,
              !value.displayName.contains("\0")
        else { throw FileSubscriptionStoreError.invalidState }
    }

    private static func validIdentifier(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= 128
            && value.unicodeScalars.allSatisfy {
                CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
                    .contains($0)
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
                throw FileSubscriptionStoreError.stateUnavailable
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        } catch let error as FileSubscriptionStoreError {
            throw error
        } catch {
            throw FileSubscriptionStoreError.stateUnavailable
        }
    }
}
