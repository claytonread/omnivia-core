import CryptoKit
import Foundation

public enum FolderCaptureMode: String, Sendable {
    case importOnce
    case keepUpdated
}

public enum FolderWatchInitialContent: String, Codable, Sendable {
    case includeExisting = "include_existing"
    case onlyNewAndChanged = "only_new_and_changed"
}

public struct FolderCapturePolicy: Equatable, Sendable {
    public let mode: FolderCaptureMode
    public let recursive: Bool
    public let includeHidden: Bool
    public let watchInitialContent: FolderWatchInitialContent

    public init(
        mode: FolderCaptureMode,
        recursive: Bool,
        includeHidden: Bool,
        watchInitialContent: FolderWatchInitialContent = .includeExisting
    ) {
        self.mode = mode
        self.recursive = recursive
        self.includeHidden = includeHidden
        self.watchInitialContent = watchInitialContent
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
    case duplicateWatch
    case watchLimitExceeded
}

/// Reviews and freezes one selected folder without traversing symlinks or packages.
/// Import-once and the initial Keep Updated baseline share the durable v3 queue;
/// subsequent observations use the separately persisted subscription scanner.
public final class FolderCaptureSubmission: @unchecked Sendable {
    public static let maximumMembers = 10_000
    public static let maximumAggregateBytes: UInt64 = 2 * 1024 * 1024 * 1024
    public static let maximumExaminedEntries = 100_000
    public static let maximumRecursionDepth = 32
    public static let maximumActiveWatches = 20

    private static let excludedDirectoryNames: Set<String> = [
        ".git", ".venv", "node_modules", "__pycache__", "DerivedData",
    ]

    private let accessStore: NativeAccessStore
    private let reader: any CoordinatedOriginalReading
    private let subscriptions: FolderSubscriptionStore
    private let bulkRuns: FolderBulkRunStore
    private let bulkCoordinator: FolderBulkRunCoordinator
    /// One source of truth for the target, workspace and caller facts frozen
    /// into every intent this submission queues; delivery requires an exact
    /// match against the transport's binding.
    private let binding: CaptureDeliveryBinding

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
        self.reader = environment.reader
        subscriptions = environment.folderSubscriptions
        bulkRuns = environment.folderBulkRuns
        bulkCoordinator = environment.folderBulkCoordinator
        binding = .companion(workspaceRef: workspaceRef, targetRef: targetRef)
    }

    public init(
        environment: DocumentIntakeEnvironment,
        workspaceRef: String,
        targetRef: String = "local"
    ) {
        accessStore = environment.accessStore
        reader = environment.reader
        subscriptions = environment.folderSubscriptions
        bulkRuns = environment.folderBulkRuns
        bulkCoordinator = environment.folderBulkCoordinator
        binding = .companion(workspaceRef: workspaceRef, targetRef: targetRef)
    }

    public init(
        environment: DocumentIntakeEnvironment,
        binding: CaptureDeliveryBinding
    ) {
        accessStore = environment.accessStore
        reader = environment.reader
        subscriptions = environment.folderSubscriptions
        bulkRuns = environment.folderBulkRuns
        bulkCoordinator = environment.folderBulkCoordinator
        self.binding = binding
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
        if members.isEmpty,
           !(policy.mode == .keepUpdated && policy.watchInitialContent == .onlyNewAndChanged)
        {
            throw FolderCaptureSubmissionError.emptyScope
        }
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
        if plan.policy.mode == .keepUpdated {
            let watched = subscriptions.all().filter { $0.state != .stopped }
            guard watched.count < Self.maximumActiveWatches else {
                throw FolderCaptureSubmissionError.watchLimitExceeded
            }
            let requestedPath = plan.root.resolvingSymlinksInPath().standardizedFileURL.path
            let duplicate = watched.contains { subscription in
                guard subscription.targetRef == binding.targetRef,
                      subscription.workspaceRef == binding.workspaceRef
                else { return false }
                let generation = subscription.members.map(\.membershipGeneration).min() ?? 1
                return (try? accessStore.withRootReadAccess(
                    rootRef: subscription.rootRef,
                    expectedGeneration: generation
                ) { root in
                    root.resolvingSymlinksInPath().standardizedFileURL.path == requestedPath
                }) == true
            }
            guard !duplicate else { throw FolderCaptureSubmissionError.duplicateWatch }
        }
        let submissionID = Self.identifier(prefix: "submission")
        let rootRef = Self.identifier(prefix: "root")
        let bulkRunID = Self.identifier(prefix: "bulk")
        let manifestRevision = Self.identifier(prefix: "manifest")
        let subscriptionID = Self.identifier(prefix: "subscription")
        let createdAt = ISO8601DateFormatter().string(from: Date())
        let keepsUpdated = plan.policy.mode == .keepUpdated
        let importsExisting = !keepsUpdated
            || plan.policy.watchInitialContent != .onlyNewAndChanged
        let hasBulkRun = importsExisting && !plan.members.isEmpty
        if !keepsUpdated && !hasBulkRun { return [] }
        let firstOwner = keepsUpdated ? subscriptionID : bulkRunID
        let root = try accessStore.adoptRoot(
            rootRef: rootRef,
            ownerRef: firstOwner,
            selectedURL: plan.root
        )
        if keepsUpdated && hasBulkRun {
            _ = try accessStore.adoptRoot(
                rootRef: rootRef,
                ownerRef: bulkRunID,
                selectedURL: plan.root
            )
        }
        var runMembers: [FolderBulkRunMember] = []
        var subscriptionMembers: [FolderSubscriptionMember] = []
        for member in plan.members {
            let memberID = Self.identifier(prefix: "member")
            let itemID = Self.identifier(prefix: "item")
            let documentID = Self.identifier(prefix: "doc")
            let accessRef = Self.identifier(prefix: "access")
            let descriptor = try accessStore.adoptRootMember(
                accessRef: accessRef,
                linkedDocumentID: documentID,
                rootRef: rootRef,
                memberRef: memberID,
                relativeComponents: member.relativeComponents,
                expectedRootGeneration: root.generation
            )
            let automatic = plan.policy.mode == .keepUpdated
            let sourceIdentity = Self.identifier(prefix: "source")
            runMembers.append(
                FolderBulkRunMember(
                    memberID: memberID,
                    accessRef: descriptor.accessRef,
                    linkedDocumentID: documentID,
                    requestedSourceIdentity: sourceIdentity,
                    relativeComponents: member.relativeComponents,
                    displayName: member.displayName,
                    contentKind: member.contentKind,
                    bindingGeneration: descriptor.generation,
                    itemID: itemID,
                    payloadRef: Self.identifier(prefix: "payload"),
                    operationKey: Self.identifier(prefix: "capture"),
                    declaredByteLength: member.byteLength,
                    state: .planned,
                    failureCode: nil
                )
            )
            if automatic {
                let excludedBaseline = plan.policy.watchInitialContent == .onlyNewAndChanged
                let baselineBytes = excludedBaseline
                    ? try accessStore.withReadAccess(
                        accessRef: descriptor.accessRef,
                        expectedGeneration: descriptor.generation,
                        expectedRootBindingGeneration: descriptor.rootBindingGeneration
                    ) { url in
                        try reader.readStableBytes(
                            from: url,
                            maximumBytes: CaptureIntentV3.maximumPayloadBytes
                        )
                    }
                    : nil
                subscriptionMembers.append(
                    FolderSubscriptionMember(
                        memberID: memberID,
                        accessRef: descriptor.accessRef,
                        linkedDocumentID: documentID,
                        requestedSourceIdentity: sourceIdentity,
                        relativeComponents: member.relativeComponents,
                        displayName: member.displayName,
                        contentKind: member.contentKind,
                        membershipGeneration: descriptor.generation,
                        baselineItemID: excludedBaseline ? nil : itemID,
                        currentSuccessfulCaptureID: nil,
                        lastObservedDigest: baselineBytes.map(Self.digest),
                        lastObservedByteLength: baselineBytes.map { UInt64($0.count) },
                        lastObservedAt: excludedBaseline ? createdAt : nil,
                        lastQueuedItemID: excludedBaseline ? nil : itemID,
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
                    targetRef: binding.targetRef,
                    workspaceRef: binding.workspaceRef,
                    targetBindingRevision: binding.targetBindingRevision,
                    callerContextBinding: binding.callerContextBinding,
                    recursive: plan.policy.recursive,
                    includeHidden: plan.policy.includeHidden,
                    configGeneration: 1,
                    consentGeneration: 1,
                    consentRecordedAt: createdAt,
                    observationEpoch: Self.identifier(prefix: "observation"),
                    state: plan.policy.watchInitialContent == .onlyNewAndChanged
                        ? .active : .waitingForCore,
                    needsFullReconciliation: true,
                    lastCompletedMetadataScanAt: nil,
                    pendingReviewSignature: nil,
                    approvedReviewSignature: nil,
                    reviewChangeCount: nil,
                    reviewExamples: nil,
                    members: subscriptionMembers
                )
            )
        }
        if plan.policy.mode == .keepUpdated,
           plan.policy.watchInitialContent == .onlyNewAndChanged
        {
            return []
        }
        if hasBulkRun {
            _ = try bulkRuns.register(
                FolderBulkRun(
                runID: bulkRunID,
                manifestRevision: manifestRevision,
                submissionID: submissionID,
                rootRef: rootRef,
                rootDisplayName: plan.root.lastPathComponent,
                subscriptionID: plan.policy.mode == .keepUpdated ? subscriptionID : nil,
                targetRef: binding.targetRef,
                workspaceRef: binding.workspaceRef,
                targetBindingRevision: binding.targetBindingRevision,
                callerContextBinding: binding.callerContextBinding,
                createdAt: createdAt,
                state: .adding,
                    members: runMembers
                )
            )
            return await bulkCoordinator.advance(runID: bulkRunID).newlyPublished
        }
        return []
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

    private static func digest(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
