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
    public let skippedExcluded: Int
    public let skippedUnavailable: Int
    public let examinedEntries: Int
    public var totalBytes: UInt64 { members.reduce(0) { $0 + $1.byteLength } }
}

public enum FolderCaptureSubmissionError: Error, Equatable {
    case rootUnavailable
    case tooManyMembers
    case aggregateTooLarge
    case emptyScope
    case unsafeMember
    case unsafeRoot
    case scanLimitExceeded
}

/// Reviews and freezes one selected folder without traversing symlinks or packages.
/// Import-once and the initial Keep Updated baseline share the durable v3 queue;
/// subsequent observations use the separately persisted subscription scanner.
public final class FolderCaptureSubmission: @unchecked Sendable {
    public static let maximumMembers = 10_000
    public static let maximumAggregateBytes: UInt64 = 2 * 1024 * 1024 * 1024
    public static let maximumExaminedEntries = 100_000
    public static let maximumRecursionDepth = 32

    private static let excludedDirectoryNames: Set<String> = [
        ".git", ".venv", "node_modules", "__pycache__", "DerivedData",
    ]

    private let accessStore: NativeAccessStore
    private let preparation: CapturePreparationCoordinator
    private let subscriptions: FolderSubscriptionStore
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
        subscriptions = environment.folderSubscriptions
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
        subscriptions = environment.folderSubscriptions
        self.workspaceRef = workspaceRef
        self.targetRef = targetRef
    }

    public func plan(root: URL, policy: FolderCapturePolicy) throws -> FolderCapturePlan {
        let rootValues = try root.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
        guard root.isFileURL, rootValues.isDirectory == true, rootValues.isSymbolicLink != true else {
            throw FolderCaptureSubmissionError.rootUnavailable
        }
        let canonicalRoot = root.resolvingSymlinksInPath().standardizedFileURL
        let home = FileManager.default.homeDirectoryForCurrentUser
            .resolvingSymlinksInPath().standardizedFileURL
        guard canonicalRoot.path != "/", canonicalRoot != home else {
            throw FolderCaptureSubmissionError.unsafeRoot
        }
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
        var skippedExcluded = 0
        var skippedUnavailable = 0
        var examinedEntries = 0
        var totalBytes: UInt64 = 0
        for case let candidate as URL in enumerator {
            examinedEntries += 1
            guard examinedEntries <= Self.maximumExaminedEntries else {
                throw FolderCaptureSubmissionError.scanLimitExceeded
            }
            guard let values = try? candidate.resourceValues(forKeys: keys) else {
                skippedUnavailable += 1
                continue
            }
            if values.isSymbolicLink == true {
                if values.isDirectory == true { enumerator.skipDescendants() }
                continue
            }
            let relative = try Self.relativeComponents(root: canonicalRoot, member: candidate)
            if relative.count > Self.maximumRecursionDepth {
                skippedExcluded += values.isRegularFile == true ? 1 : 0
                if values.isDirectory == true { enumerator.skipDescendants() }
                continue
            }
            if !policy.recursive, relative.count > 1 {
                if values.isDirectory == true { enumerator.skipDescendants() }
                continue
            }
            if !policy.includeHidden, values.isHidden == true {
                skippedHidden += values.isRegularFile == true ? 1 : 0
                if values.isDirectory == true { enumerator.skipDescendants() }
                continue
            }
            if Self.isDefaultExcluded(relativeComponents: relative, isDirectory: values.isDirectory == true) {
                skippedExcluded += values.isRegularFile == true ? 1 : 0
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
            skippedHidden: skippedHidden,
            skippedExcluded: skippedExcluded,
            skippedUnavailable: skippedUnavailable,
            examinedEntries: examinedEntries
        )
    }

    private static func isDefaultExcluded(
        relativeComponents: [String],
        isDirectory: Bool
    ) -> Bool {
        guard let name = relativeComponents.last else { return true }
        if isDirectory, excludedDirectoryNames.contains(name) { return true }
        if name == ".DS_Store" || name.hasPrefix("~$") { return true }
        let lower = name.lowercased()
        return [".swp", ".swo", ".tmp", ".part", ".crdownload"].contains {
            lower.hasSuffix($0)
        }
    }

    public func submit(_ plan: FolderCapturePlan) async throws -> [CaptureQueueEntry] {
        let submissionID = Self.identifier(prefix: "submission")
        let rootRef = Self.identifier(prefix: "root")
        let bulkRunID = Self.identifier(prefix: "bulk")
        let manifestRevision = Self.identifier(prefix: "manifest")
        let subscriptionID = Self.identifier(prefix: "subscription")
        let createdAt = ISO8601DateFormatter().string(from: Date())
        var items: [CapturePreparationItem] = []
        var subscriptionMembers: [FolderSubscriptionMember] = []
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
            if automatic {
                subscriptionMembers.append(
                    FolderSubscriptionMember(
                        memberID: memberID,
                        accessRef: descriptor.accessRef,
                        linkedDocumentID: documentID,
                        requestedSourceIdentity: seed.requestedSourceIdentity,
                        relativeComponents: member.relativeComponents,
                        displayName: member.displayName,
                        contentKind: member.contentKind,
                        membershipGeneration: descriptor.generation,
                        baselineItemID: itemID,
                        currentSuccessfulCaptureID: nil,
                        lastObservedDigest: nil,
                        lastObservedByteLength: nil,
                        lastObservedAt: nil,
                        lastQueuedItemID: itemID,
                        dirtyEpoch: 1,
                        state: .present
                    )
                )
            }
        }
        if plan.policy.mode == .keepUpdated {
            _ = try subscriptions.register(
                FolderSubscription(
                    subscriptionID: subscriptionID,
                    rootRef: rootRef,
                    rootDisplayName: plan.root.lastPathComponent,
                    targetRef: targetRef,
                    workspaceRef: workspaceRef,
                    targetBindingRevision: "1",
                    callerContextBinding: CallerContextBinding(
                        clientContextRef: "macos-companion",
                        principalRef: "native-user",
                        contextRevision: "1"
                    ),
                    recursive: plan.policy.recursive,
                    includeHidden: plan.policy.includeHidden,
                    configGeneration: 1,
                    consentGeneration: 1,
                    consentRecordedAt: createdAt,
                    observationEpoch: Self.identifier(prefix: "observation"),
                    state: .waitingForCore,
                    needsFullReconciliation: true,
                    lastCompletedMetadataScanAt: nil,
                    members: subscriptionMembers
                )
            )
        }
        let entries = try await preparation.prepareAndPublish(items)
        if plan.policy.mode == .keepUpdated {
            let byItem = Dictionary(uniqueKeysWithValues: entries.map { ($0.intent.itemId, $0) })
            _ = try subscriptions.update(id: subscriptionID) { subscription in
                for index in subscription.members.indices {
                    if let entry = byItem[subscription.members[index].baselineItemID] {
                        subscription.members[index].lastObservedDigest = entry.intent.localIntegrityDigest
                        subscription.members[index].lastObservedByteLength = entry.intent.byteLength
                        subscription.members[index].lastObservedAt = entry.intent.createdAt
                    }
                }
            }
        }
        return entries
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
