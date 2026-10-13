import Darwin
import Foundation

public enum FolderBulkMemberState: String, Codable, Sendable {
    case planned
    case published
    case admitted
    case needsAttention = "needs_attention"
    case stopped
}

public enum FolderBulkRunState: String, Codable, Sendable {
    case adding
    case paused
    case complete
    case needsAttention = "needs_attention"
    case stopped
}

public struct FolderBulkRunMember: Codable, Equatable, Sendable {
    public let memberID: String
    public let accessRef: String
    public let linkedDocumentID: String
    public let requestedSourceIdentity: String
    public let relativeComponents: [String]
    public let displayName: String
    public let contentKind: CaptureContentKind
    public let bindingGeneration: UInt64
    public let itemID: String
    public let payloadRef: String
    public let operationKey: String
    public let declaredByteLength: UInt64
    public var state: FolderBulkMemberState
    public var failureCode: String?
}

public struct FolderBulkRun: Codable, Equatable, Sendable {
    public let runID: String
    public let manifestRevision: String
    public let submissionID: String
    public let rootRef: String
    public let rootDisplayName: String
    public let subscriptionID: String?
    public let targetRef: String
    public let workspaceRef: String
    public let targetBindingRevision: String
    public let callerContextBinding: CallerContextBinding
    public let createdAt: String
    public var state: FolderBulkRunState
    public var members: [FolderBulkRunMember]

    public var plannedCount: Int { members.filter { $0.state == .planned }.count }
    public var publishedCount: Int { members.filter { $0.state == .published }.count }
    public var admittedCount: Int { members.filter { $0.state == .admitted }.count }
    public var attentionCount: Int { members.filter { $0.state == .needsAttention }.count }
}

public enum FolderBulkRunStoreError: Error, Equatable {
    case duplicateRun
    case unknownRun
    case invalidState
    case stateUnavailable
}

private struct FolderBulkRunDocument: Codable {
    let schema: String
    let runs: [FolderBulkRun]
}

/// Durable frozen folder membership. Absolute paths and source bytes remain in the access/queue
/// owners; this ledger stores only opaque identities and root-relative locators.
public final class FolderBulkRunStore: @unchecked Sendable {
    private static let schema = "omnivia.folder-bulk-runs.v1"
    private static let maximumStateBytes = 256 * 1024 * 1024
    private let lock = NSLock()
    private let directory: URL
    private let stateFile: URL
    private var runs: [String: FolderBulkRun]

    public init(stateDirectory: URL) throws {
        directory = stateDirectory
        stateFile = stateDirectory.appendingPathComponent("folder-bulk-runs-v1.json")
        try Self.prepareDirectory(stateDirectory)
        runs = try Self.load(stateFile)
    }

    public func all() -> [FolderBulkRun] {
        lock.withLock { runs.values.sorted { $0.createdAt < $1.createdAt } }
    }

    public func run(id: String) -> FolderBulkRun? { lock.withLock { runs[id] } }

    @discardableResult
    public func register(_ run: FolderBulkRun) throws -> FolderBulkRun {
        try lock.withLock {
            guard runs[run.runID] == nil else { throw FolderBulkRunStoreError.duplicateRun }
            try Self.validate(run)
            var replacement = runs
            replacement[run.runID] = run
            try persist(replacement)
            runs = replacement
            return run
        }
    }

    @discardableResult
    public func update(
        id: String,
        _ change: (inout FolderBulkRun) throws -> Void
    ) throws -> FolderBulkRun {
        try lock.withLock {
            guard var value = runs[id] else { throw FolderBulkRunStoreError.unknownRun }
            try change(&value)
            try Self.validate(value)
            var replacement = runs
            replacement[id] = value
            try persist(replacement)
            runs = replacement
            return value
        }
    }

    private func persist(_ values: [String: FolderBulkRun]) throws {
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
            let data = try encoder.encode(
                FolderBulkRunDocument(
                    schema: Self.schema,
                    runs: values.values.sorted { $0.createdAt < $1.createdAt }
                )
            )
            guard data.count <= Self.maximumStateBytes else {
                throw FolderBulkRunStoreError.stateUnavailable
            }
            let temporary = directory.appendingPathComponent(".folder-bulk-runs-\(UUID().uuidString).tmp")
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
        } catch let error as FolderBulkRunStoreError {
            throw error
        } catch {
            throw FolderBulkRunStoreError.stateUnavailable
        }
    }

    private static func load(_ stateFile: URL) throws -> [String: FolderBulkRun] {
        guard FileManager.default.fileExists(atPath: stateFile.path) else { return [:] }
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: stateFile.path)
            guard (attributes[.type] as? FileAttributeType) == .typeRegular,
                  let size = attributes[.size] as? NSNumber,
                  size.intValue <= maximumStateBytes,
                  let owner = attributes[.ownerAccountID] as? NSNumber,
                  owner.uint32Value == Darwin.getuid(),
                  let permissions = attributes[.posixPermissions] as? NSNumber,
                  permissions.intValue & 0o077 == 0
            else { throw FolderBulkRunStoreError.invalidState }
            let document = try JSONDecoder().decode(
                FolderBulkRunDocument.self,
                from: Data(contentsOf: stateFile, options: [.mappedIfSafe])
            )
            guard document.schema == schema else { throw FolderBulkRunStoreError.invalidState }
            var result: [String: FolderBulkRun] = [:]
            for run in document.runs {
                try validate(run)
                guard result[run.runID] == nil else { throw FolderBulkRunStoreError.invalidState }
                result[run.runID] = run
            }
            return result
        } catch let error as FolderBulkRunStoreError {
            throw error
        } catch {
            throw FolderBulkRunStoreError.invalidState
        }
    }

    private static func validate(_ run: FolderBulkRun) throws {
        let identifiers = [
            run.runID, run.manifestRevision, run.submissionID, run.rootRef,
            run.targetRef, run.workspaceRef, run.targetBindingRevision,
        ] + (run.subscriptionID.map { [$0] } ?? [])
        guard identifiers.allSatisfy(validIdentifier),
              !run.rootDisplayName.isEmpty,
              run.rootDisplayName.count <= 255,
              !run.members.isEmpty,
              run.members.count <= FolderCaptureSubmission.maximumMembers,
              Set(run.members.map(\.memberID)).count == run.members.count,
              Set(run.members.map(\.accessRef)).count == run.members.count,
              Set(run.members.map(\.itemID)).count == run.members.count,
              run.members.allSatisfy({ member in
                  [member.memberID, member.accessRef, member.linkedDocumentID,
                   member.requestedSourceIdentity, member.itemID, member.payloadRef,
                   member.operationKey].allSatisfy(validIdentifier)
                      && member.bindingGeneration > 0
                      && member.declaredByteLength > 0
                      && member.declaredByteLength <= CaptureIntentV3.maximumPayloadBytes
                      && !member.relativeComponents.isEmpty
                      && member.relativeComponents.allSatisfy { !$0.isEmpty && $0 != "." && $0 != ".." }
              })
        else { throw FolderBulkRunStoreError.invalidState }
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
                throw FolderBulkRunStoreError.stateUnavailable
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        } catch let error as FolderBulkRunStoreError {
            throw error
        } catch {
            throw FolderBulkRunStoreError.stateUnavailable
        }
    }
}
