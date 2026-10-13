import Foundation

public struct ManualCaptureCandidate: Sendable, Equatable {
    public enum Source: Sendable, Equatable {
        case original
        case capturedSnapshot
    }

    public let url: URL
    public let displayName: String
    public let surface: CaptureSurface
    public let keepUpdated: Bool
    public let source: Source

    public init(
        url: URL,
        displayName: String? = nil,
        surface: CaptureSurface = .manual,
        keepUpdated: Bool = false,
        source: Source = .original
    ) {
        self.url = url
        self.displayName = displayName ?? url.lastPathComponent
        self.surface = surface
        self.keepUpdated = keepUpdated
        self.source = source
    }
}

public enum ManualCaptureSubmissionError: Error, Equatable {
    case emptySelection
    case tooManyItems
    case unsupportedType
    case duplicateAutomaticSource
    case automaticSourceLimitExceeded
    case snapshotCannotBeMonitored
}

/// Converts one reviewed native file selection into the durable v3 queue.
/// Picker URLs are adopted as read-only originals; bytes are coordinated and
/// the complete ordinary batch is published atomically.
public final class ManualCaptureSubmission: @unchecked Sendable {
    public static let maximumItems = 10
    public static let maximumAutomaticFiles = 1_000

    private let accessStore: NativeAccessStore
    private let preparation: CapturePreparationCoordinator
    private let subscriptions: FileSubscriptionStore
    private let snapshotStore: OwnedSnapshotStore?
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
        self.accessStore = environment.accessStore
        preparation = environment.preparation
        subscriptions = environment.fileSubscriptions
        snapshotStore = try OwnedSnapshotStore(
            root: stateRoot.appendingPathComponent("owned-snapshots", isDirectory: true)
        )
        self.workspaceRef = workspaceRef
        self.targetRef = targetRef
    }

    public init(
        environment: DocumentIntakeEnvironment,
        workspaceRef: String,
        targetRef: String = "local",
        snapshotStore: OwnedSnapshotStore? = nil
    ) {
        accessStore = environment.accessStore
        preparation = environment.preparation
        subscriptions = environment.fileSubscriptions
        self.snapshotStore = snapshotStore
        self.workspaceRef = workspaceRef
        self.targetRef = targetRef
    }

    public func submit(_ candidates: [ManualCaptureCandidate]) async throws -> [CaptureQueueEntry] {
        guard !candidates.isEmpty else { throw ManualCaptureSubmissionError.emptySelection }
        guard candidates.count <= Self.maximumItems else { throw ManualCaptureSubmissionError.tooManyItems }
        let automaticCandidates = candidates.filter(\.keepUpdated)
        guard !automaticCandidates.contains(where: { $0.source == .capturedSnapshot }) else {
            throw ManualCaptureSubmissionError.snapshotCannotBeMonitored
        }
        let existingAutomatic = subscriptions.all().filter { $0.state != .stopped }
        guard existingAutomatic.count + automaticCandidates.count <= Self.maximumAutomaticFiles else {
            throw ManualCaptureSubmissionError.automaticSourceLimitExceeded
        }
        let existingPaths = Set<String>(existingAutomatic.compactMap { subscription -> String? in
            guard subscription.targetRef == targetRef,
                  subscription.workspaceRef == workspaceRef
            else { return nil }
            return try? accessStore.withReadAccess(
                accessRef: subscription.accessRef,
                expectedGeneration: subscription.bindingGeneration
            ) { $0.resolvingSymlinksInPath().standardizedFileURL.path }
        })
        let requestedPaths = automaticCandidates.map {
            $0.url.resolvingSymlinksInPath().standardizedFileURL.path
        }
        guard Set(requestedPaths).count == requestedPaths.count,
              existingPaths.isDisjoint(with: requestedPaths)
        else { throw ManualCaptureSubmissionError.duplicateAutomaticSource }
        let submissionID = Self.identifier(prefix: "submission")
        let createdAt = ISO8601DateFormatter().string(from: Date())
        var items: [CapturePreparationItem] = []
        var pendingSubscriptions: [FileSubscription] = []
        var snapshotAccessRefs: [String] = []
        defer {
            for accessRef in snapshotAccessRefs {
                _ = try? accessStore.disconnect(accessRef: accessRef)
            }
        }
        for candidate in candidates {
            guard let kind = Self.contentKind(for: candidate.url) else {
                throw ManualCaptureSubmissionError.unsupportedType
            }
            let itemID = Self.identifier(prefix: "item")
            let documentID = Self.identifier(prefix: "doc")
            let accessRef = Self.identifier(prefix: "access")
            let descriptor = try accessStore.adopt(
                accessRef: accessRef,
                linkedDocumentID: documentID,
                selectedURL: candidate.url,
                route: .directFile
            )
            if candidate.source == .capturedSnapshot { snapshotAccessRefs.append(descriptor.accessRef) }
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
                surface: candidate.surface,
                contentKind: kind,
                payloadRef: Self.identifier(prefix: "payload"),
                requestedSourceIdentity: Self.identifier(prefix: "source"),
                operationKey: Self.identifier(prefix: "capture"),
                intentKind: .newCapture,
                basePrecondition: .create,
                processingProfile: ProcessingProfileBinding(
                    profileRef: "document-default",
                    revision: "1"
                ),
                originalBinding: candidate.source == .original
                    ? OriginalBindingIntent(
                        accessRecordId: descriptor.accessRef,
                        bindingGeneration: descriptor.generation,
                        linkedDocumentId: documentID
                    )
                    : nil,
                createdAt: createdAt,
                displayName: candidate.displayName
            )
            items.append(CapturePreparationItem(
                access: NativeAccessExpectation(
                    accessRef: descriptor.accessRef,
                    generation: descriptor.generation
                ),
                seed: seed
            ))
            if candidate.keepUpdated {
                let subscription = FileSubscription(
                    subscriptionID: Self.identifier(prefix: "subscription"),
                    accessRef: descriptor.accessRef,
                    linkedDocumentID: documentID,
                    requestedSourceIdentity: seed.requestedSourceIdentity,
                    displayName: candidate.displayName,
                    contentKind: kind,
                    bindingGeneration: descriptor.generation,
                    baselineItemID: itemID,
                    targetRef: targetRef,
                    workspaceRef: workspaceRef,
                    targetBindingRevision: "1",
                    callerContextBinding: seed.callerContextBinding,
                    configGeneration: 1,
                    consentGeneration: 1,
                    consentRecordedAt: createdAt,
                    state: .waitingForCore,
                    currentSuccessfulCaptureID: nil,
                    lastObservedDigest: nil,
                    lastObservedByteLength: nil,
                    lastObservedAt: nil,
                    lastQueuedItemID: itemID,
                    dirtyEpoch: 1,
                    needsReconciliation: true,
                    lastCheckedAt: nil
                )
                _ = try subscriptions.register(subscription)
                pendingSubscriptions.append(subscription)
            }
        }
        let entries = try await preparation.prepareAndPublish(items)
        snapshotStore?.remove(candidates.filter { $0.source == .capturedSnapshot }.map(\.url))
        let byItem = Dictionary(uniqueKeysWithValues: entries.map { ($0.intent.itemId, $0) })
        for subscription in pendingSubscriptions {
            guard let entry = byItem[subscription.baselineItemID] else { continue }
            _ = try subscriptions.update(id: subscription.subscriptionID) {
                $0.lastObservedDigest = entry.intent.localIntegrityDigest
                $0.lastObservedByteLength = entry.intent.byteLength
                $0.lastObservedAt = entry.intent.createdAt
            }
        }
        return entries
    }

    public static func contentKind(for url: URL) -> CaptureContentKind? {
        switch url.pathExtension.lowercased() {
        case "pdf": .pdf
        case "docx": .docx
        case "xlsx": .xlsx
        case "pptx": .pptx
        case "csv": .csv
        case "tsv": .tsv
        case "rtf": .rtf
        case "html", "htm": .html
        case "md", "markdown": .markdown
        case "txt": .text
        case "png", "jpg", "jpeg", "heic", "tif", "tiff", "webp": .image
        default: nil
        }
    }

    private static func identifier(prefix: String) -> String {
        "\(prefix)-\(UUID().uuidString.lowercased())"
    }
}
