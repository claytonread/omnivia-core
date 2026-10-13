import Darwin
import Foundation

public enum FolderSubscriptionState: String, Codable, Sendable {
    case waitingForCore = "waiting_for_core"
    case active
    case paused
    case reviewRequired = "review_required"
    case needsAccess = "needs_access"
    case stopped
}

public enum FolderMemberState: String, Codable, Sendable {
    case present
    case absent
}

public struct FolderSubscriptionMember: Codable, Equatable, Sendable {
    public let memberID: String
    public let accessRef: String
    public let linkedDocumentID: String
    public let requestedSourceIdentity: String
    public let relativeComponents: [String]
    public let displayName: String
    public let contentKind: CaptureContentKind
    public let membershipGeneration: UInt64
    public let baselineItemID: String?
    public var currentSuccessfulCaptureID: String?
    public var lastObservedDigest: String?
    public var lastObservedByteLength: UInt64?
    public var lastObservedAt: String?
    public var lastQueuedItemID: String?
    public var dirtyEpoch: UInt64
    public var state: FolderMemberState
}

public struct FolderSubscription: Codable, Equatable, Sendable {
    public let subscriptionID: String
    public let rootRef: String
    public var rootBindingGeneration: UInt64 = 1
    public let rootDisplayName: String
    public let targetRef: String
    public let workspaceRef: String
    public let targetBindingRevision: String
    public let callerContextBinding: CallerContextBinding
    public let recursive: Bool
    public let includeHidden: Bool
    public let configGeneration: UInt64
    public let consentGeneration: UInt64
    public let consentRecordedAt: String
    public let observationEpoch: String
    public var state: FolderSubscriptionState
    public var needsFullReconciliation: Bool
    public var lastCompletedMetadataScanAt: String?
    public var pendingReviewSignature: String?
    public var approvedReviewSignature: String?
    public var reviewChangeCount: Int?
    public var reviewExamples: [String]?
    public var members: [FolderSubscriptionMember]
}

public enum FolderSubscriptionStoreError: Error, Equatable {
    case duplicateSubscription
    case unknownSubscription
    case invalidState
    case stateUnavailable
}

private struct FolderSubscriptionDocument: Codable {
    let schema: String
    let subscriptions: [FolderSubscription]
}

/// Owner-only durable monitoring state. It stores opaque access references and root-relative
/// locators; the selected root's bookmark and absolute path remain in `NativeAccessStore`.
public final class FolderSubscriptionStore: @unchecked Sendable {
    private static let schema = "omnivia.folder-subscriptions.v1"
    /// Shared with the other publication owners; see `PublicationGate`.
    private let gate: PublicationGate
    private let directory: URL
    private let stateFile: URL
    private var subscriptions: [String: FolderSubscription]

    public init(stateDirectory: URL, gate: PublicationGate = .processShared) throws {
        self.gate = gate
        directory = stateDirectory
        stateFile = stateDirectory.appendingPathComponent("folder-subscriptions-v1.json")
        try Self.prepareDirectory(stateDirectory)
        subscriptions = try Self.load(stateFile)
    }

    public func all() -> [FolderSubscription] {
        gate.withLock { subscriptions.values.sorted { $0.subscriptionID < $1.subscriptionID } }
    }

    public func subscription(id: String) -> FolderSubscription? {
        gate.withLock { subscriptions[id] }
    }

    @discardableResult
    public func register(_ subscription: FolderSubscription) throws -> FolderSubscription {
        try gate.withLock {
            guard subscriptions[subscription.subscriptionID] == nil else {
                throw FolderSubscriptionStoreError.duplicateSubscription
            }
            try Self.validate(subscription)
            var replacement = subscriptions
            replacement[subscription.subscriptionID] = subscription
            try persist(replacement)
            subscriptions = replacement
            return subscription
        }
    }

    @discardableResult
    public func update(
        id: String,
        _ change: (inout FolderSubscription) throws -> Void
    ) throws -> FolderSubscription {
        try gate.withLock {
            guard var current = subscriptions[id] else {
                throw FolderSubscriptionStoreError.unknownSubscription
            }
            let frozenRootBindingGeneration = current.rootBindingGeneration
            try change(&current)
            // The adopted root grant generation is frozen for the life of the subscription.
            guard current.rootBindingGeneration == frozenRootBindingGeneration else {
                throw FolderSubscriptionStoreError.invalidState
            }
            try Self.validate(current)
            var replacement = subscriptions
            replacement[id] = current
            try persist(replacement)
            subscriptions = replacement
            return current
        }
    }

    private func persist(_ values: [String: FolderSubscription]) throws {
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
            let data = try encoder.encode(
                FolderSubscriptionDocument(
                    schema: Self.schema,
                    subscriptions: values.values.sorted { $0.subscriptionID < $1.subscriptionID }
                )
            )
            guard data.count <= 16 * 1024 * 1024 else {
                throw FolderSubscriptionStoreError.stateUnavailable
            }
            let temporary = directory.appendingPathComponent(".folder-subscriptions-\(UUID().uuidString).tmp")
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
        } catch let error as FolderSubscriptionStoreError {
            throw error
        } catch {
            throw FolderSubscriptionStoreError.stateUnavailable
        }
    }

    private static func load(_ stateFile: URL) throws -> [String: FolderSubscription] {
        guard FileManager.default.fileExists(atPath: stateFile.path) else { return [:] }
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: stateFile.path)
            guard (attributes[.type] as? FileAttributeType) == .typeRegular,
                  let size = attributes[.size] as? NSNumber,
                  size.uint64Value <= 16 * 1024 * 1024,
                  let owner = attributes[.ownerAccountID] as? NSNumber,
                  owner.uint32Value == Darwin.getuid(),
                  let permissions = attributes[.posixPermissions] as? NSNumber,
                  permissions.intValue & 0o077 == 0
            else { throw FolderSubscriptionStoreError.invalidState }
            let data = try Self.addLegacyRootBindingGeneration(
                to: Data(contentsOf: stateFile, options: [.mappedIfSafe])
            )
            let document = try JSONDecoder().decode(FolderSubscriptionDocument.self, from: data)
            guard document.schema == schema else { throw FolderSubscriptionStoreError.invalidState }
            var result: [String: FolderSubscription] = [:]
            for subscription in document.subscriptions {
                try validate(subscription)
                guard result[subscription.subscriptionID] == nil else {
                    throw FolderSubscriptionStoreError.invalidState
                }
                result[subscription.subscriptionID] = subscription
            }
            return result
        } catch let error as FolderSubscriptionStoreError {
            throw error
        } catch {
            throw FolderSubscriptionStoreError.invalidState
        }
    }

    private static func validate(_ value: FolderSubscription) throws {
        let identifiers = [
            value.subscriptionID,
            value.rootRef,
            value.targetRef,
            value.workspaceRef,
            value.targetBindingRevision,
            value.observationEpoch,
        ]
        guard identifiers.allSatisfy(validIdentifier),
              value.configGeneration > 0,
              value.consentGeneration > 0,
              value.rootBindingGeneration > 0,
              value.members.count <= FolderCaptureSubmission.maximumMembers,
              !value.rootDisplayName.isEmpty,
              value.rootDisplayName.count <= 255,
              !value.rootDisplayName.contains("\0"),
              (value.reviewChangeCount.map { $0 >= 0 } ?? true),
              (value.reviewExamples?.count ?? 0) <= 10,
              (value.pendingReviewSignature.map(validDigest) ?? true),
              (value.approvedReviewSignature.map(validDigest) ?? true),
              Set(value.members.map(\.memberID)).count == value.members.count,
              Set(value.members.map(\.accessRef)).count == value.members.count,
              value.members.allSatisfy({ member in
                  validIdentifier(member.memberID)
                      && validIdentifier(member.accessRef)
                      && validIdentifier(member.linkedDocumentID)
                      && validIdentifier(member.requestedSourceIdentity)
                      && (member.baselineItemID.map(validIdentifier) ?? true)
                      && (member.baselineItemID != nil || member.lastObservedDigest.map(validDigest) == true)
                      && member.membershipGeneration > 0
                      && member.dirtyEpoch > 0
                      && !member.relativeComponents.isEmpty
                      && member.relativeComponents.allSatisfy { !$0.isEmpty && $0 != "." && $0 != ".." }
              })
        else { throw FolderSubscriptionStoreError.invalidState }
    }

    private static func addLegacyRootBindingGeneration(to data: Data) throws -> Data {
        guard var document = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              var values = document["subscriptions"] as? [[String: Any]]
        else { throw FolderSubscriptionStoreError.invalidState }
        var changed = false
        for index in values.indices where values[index]["rootBindingGeneration"] == nil {
            values[index]["rootBindingGeneration"] = 1
            changed = true
        }
        guard changed else { return data }
        document["subscriptions"] = values
        return try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
    }

    private static func validIdentifier(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= 128
            && value.unicodeScalars.allSatisfy {
                CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
                    .contains($0)
            }
    }

    private static func validDigest(_ value: String) -> Bool {
        value.hasPrefix("sha256:")
            && value.count == 71
            && value.dropFirst(7).allSatisfy { $0.isHexDigit }
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
                throw FolderSubscriptionStoreError.stateUnavailable
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        } catch let error as FolderSubscriptionStoreError {
            throw error
        } catch {
            throw FolderSubscriptionStoreError.stateUnavailable
        }
    }
}
