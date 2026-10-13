import Foundation

public enum FolderCaptureMode: String, Sendable {
    case importOnce
    case keepUpdated
}

public struct FolderCapturePolicy: Equatable, Sendable {
    public let mode: FolderCaptureMode
    public let recursive: Bool
    public let includeHidden: Bool

    public init(mode: FolderCaptureMode, recursive: Bool, includeHidden: Bool) {
        self.mode = mode
        self.recursive = recursive
        self.includeHidden = includeHidden
    }
}

public struct FolderCaptureMember: Equatable, Sendable {
    public let url: URL
    public let relativeComponents: [String]
    public let displayName: String
    public let contentKind: CaptureContentKind
    public let byteLength: UInt64
}

public struct FolderCapturePlan: Equatable, Sendable {
    public let root: URL
    public let policy: FolderCapturePolicy
    public let members: [FolderCaptureMember]
    public let skippedUnsupported: Int
    public let skippedHidden: Int
    public var totalBytes: UInt64 { members.reduce(0) { $0 + $1.byteLength } }
}

public enum FolderCaptureSubmissionError: Error, Equatable {
    case rootUnavailable
    case tooManyMembers
    case aggregateTooLarge
    case emptyScope
    case unsafeMember
}

/// Reviews and freezes one selected folder without traversing symlinks or packages.
/// Import-once and the initial Keep Updated baseline share the durable v3 queue;
/// subsequent observations use the separately persisted subscription scanner.
public final class FolderCaptureSubmission: @unchecked Sendable {
    public static let maximumMembers = 10_000
    public static let maximumAggregateBytes: UInt64 = 2 * 1024 * 1024 * 1024

    private let accessStore: NativeAccessStore
    private let preparation: CapturePreparationCoordinator
    private let workspaceRef: String
    private let targetRef: String

    public init(
        stateRoot: URL,
        workspaceRef: String,
        targetRef: String = "local",
        bookmarkCodec: any SecurityScopedBookmarkCoding = SystemSecurityScopedBookmarkCodec(),
        reader: any CoordinatedOriginalReading = SystemCoordinatedOriginalReader()
    ) throws {
        let environment = try DocumentIntakeEnvironment(
            stateRoot: stateRoot,
            bookmarkCodec: bookmarkCodec,
            reader: reader
        )
        accessStore = environment.accessStore
        preparation = environment.preparation
        self.workspaceRef = workspaceRef
        self.targetRef = targetRef
    }

    public init(
        environment: DocumentIntakeEnvironment,
        workspaceRef: String,
        targetRef: String = "local"
    ) {
        accessStore = environment.accessStore
        preparation = environment.preparation
        self.workspaceRef = workspaceRef
        self.targetRef = targetRef
    }

    public func plan(root: URL, policy: FolderCapturePolicy) throws -> FolderCapturePlan {
        let rootValues = try root.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
        guard root.isFileURL, rootValues.isDirectory == true, rootValues.isSymbolicLink != true else {
            throw FolderCaptureSubmissionError.rootUnavailable
        }
        let canonicalRoot = root.resolvingSymlinksInPath().standardizedFileURL
        let keys: Set<URLResourceKey> = [
            .isRegularFileKey,
            .isDirectoryKey,
            .isSymbolicLinkKey,
            .isHiddenKey,
            .isPackageKey,
            .fileSizeKey,
        ]
        guard let enumerator = FileManager.default.enumerator(
            at: canonicalRoot,
            includingPropertiesForKeys: Array(keys),
            options: [.skipsPackageDescendants],
            errorHandler: { _, _ in true }
        ) else { throw FolderCaptureSubmissionError.rootUnavailable }

        var members: [FolderCaptureMember] = []
        var skippedUnsupported = 0
        var skippedHidden = 0
        var totalBytes: UInt64 = 0
        for case let candidate as URL in enumerator {
            let values = try candidate.resourceValues(forKeys: keys)
            if values.isSymbolicLink == true {
                if values.isDirectory == true { enumerator.skipDescendants() }
                continue
            }
            let relative = try Self.relativeComponents(root: canonicalRoot, member: candidate)
            if !policy.recursive, relative.count > 1 {
                if values.isDirectory == true { enumerator.skipDescendants() }
                continue
            }
            if !policy.includeHidden, values.isHidden == true {
                skippedHidden += values.isRegularFile == true ? 1 : 0
                if values.isDirectory == true { enumerator.skipDescendants() }
                continue
            }
            if values.isDirectory == true { continue }
            guard values.isRegularFile == true,
                  let contentKind = ManualCaptureSubmission.contentKind(for: candidate)
            else {
                skippedUnsupported += 1
                continue
            }
            guard let length = values.fileSize, length > 0 else {
                skippedUnsupported += 1
                continue
            }
            totalBytes += UInt64(length)
            guard totalBytes <= Self.maximumAggregateBytes else {
                throw FolderCaptureSubmissionError.aggregateTooLarge
            }
            members.append(
                FolderCaptureMember(
                    url: candidate,
                    relativeComponents: relative,
                    displayName: relative.joined(separator: "/"),
                    contentKind: contentKind,
                    byteLength: UInt64(length)
                )
            )
            guard members.count <= Self.maximumMembers else {
                throw FolderCaptureSubmissionError.tooManyMembers
            }
        }
        guard !members.isEmpty else { throw FolderCaptureSubmissionError.emptyScope }
        members.sort { $0.displayName.localizedStandardCompare($1.displayName) == .orderedAscending }
        return FolderCapturePlan(
            root: canonicalRoot,
            policy: policy,
            members: members,
            skippedUnsupported: skippedUnsupported,
            skippedHidden: skippedHidden
        )
    }

    public func submit(_ plan: FolderCapturePlan) async throws -> [CaptureQueueEntry] {
        let submissionID = Self.identifier(prefix: "submission")
        let rootRef = Self.identifier(prefix: "root")
        let bulkRunID = Self.identifier(prefix: "bulk")
        let manifestRevision = Self.identifier(prefix: "manifest")
        let subscriptionID = Self.identifier(prefix: "subscription")
        let createdAt = ISO8601DateFormatter().string(from: Date())
        var items: [CapturePreparationItem] = []
        for member in plan.members {
            let memberID = Self.identifier(prefix: "member")
            let itemID = Self.identifier(prefix: "item")
            let documentID = Self.identifier(prefix: "doc")
            let accessRef = Self.identifier(prefix: "access")
            let descriptor = try accessStore.adopt(
                accessRef: accessRef,
                linkedDocumentID: documentID,
                selectedURL: plan.root,
                route: .rootMember(
                    rootRef: rootRef,
                    memberRef: memberID,
                    relativeComponents: member.relativeComponents
                )
            )
            let automatic = plan.policy.mode == .keepUpdated
            let seed = CaptureIntentSeed(
                submissionId: submissionID,
                itemId: itemID,
                targetRef: targetRef,
                workspaceRef: workspaceRef,
                targetBindingRevision: "1",
                callerContextBinding: CallerContextBinding(
                    clientContextRef: "macos-companion",
                    principalRef: "native-user",
                    contextRevision: "1"
                ),
                surface: automatic ? .automaticFolder : .folderImport,
                contentKind: member.contentKind,
                payloadRef: Self.identifier(prefix: "payload"),
                requestedSourceIdentity: Self.identifier(prefix: "source"),
                operationKey: Self.identifier(prefix: "capture"),
                intentKind: .folderCapture,
                basePrecondition: .expected(nil),
                processingProfile: ProcessingProfileBinding(
                    profileRef: "document-default",
                    revision: "1"
                ),
                originalBinding: OriginalBindingIntent(
                    accessRecordId: descriptor.accessRef,
                    bindingGeneration: descriptor.generation,
                    linkedDocumentId: documentID,
                    rootAccessGroupId: rootRef,
                    rootBindingGeneration: 1,
                    memberId: memberID,
                    membershipGeneration: 1
                ),
                consent: automatic
                    ? ConsentBinding(
                        subscriptionId: subscriptionID,
                        consentGeneration: 1,
                        configGeneration: 1,
                        dirtyEpoch: 1
                    )
                    : nil,
                bulkOrigin: BulkOriginBinding(
                    bulkRunId: bulkRunID,
                    manifestRevision: manifestRevision,
                    memberId: memberID
                ),
                createdAt: createdAt,
                displayName: member.displayName
            )
            items.append(
                CapturePreparationItem(
                    access: NativeAccessExpectation(
                        accessRef: descriptor.accessRef,
                        generation: descriptor.generation
                    ),
                    seed: seed
                )
            )
        }
        return try await preparation.prepareAndPublish(items)
    }

    private static func relativeComponents(root: URL, member: URL) throws -> [String] {
        let rootComponents = root.pathComponents
        let memberComponents = member.standardizedFileURL.pathComponents
        guard memberComponents.count > rootComponents.count,
              Array(memberComponents.prefix(rootComponents.count)) == rootComponents
        else { throw FolderCaptureSubmissionError.unsafeMember }
        let relative = Array(memberComponents.dropFirst(rootComponents.count))
        guard relative.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }) else {
            throw FolderCaptureSubmissionError.unsafeMember
        }
        return relative
    }

    private static func identifier(prefix: String) -> String {
        "\(prefix)-\(UUID().uuidString.lowercased())"
    }
}
