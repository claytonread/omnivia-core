import Foundation

public struct ManualCaptureCandidate: Sendable, Equatable {
    public let url: URL
    public let displayName: String
    public let surface: CaptureSurface
    public let keepUpdated: Bool

    public init(
        url: URL,
        displayName: String? = nil,
        surface: CaptureSurface = .manual,
        keepUpdated: Bool = false
    ) {
        self.url = url
        self.displayName = displayName ?? url.lastPathComponent
        self.surface = surface
        self.keepUpdated = keepUpdated
    }
}

public enum ManualCaptureSubmissionError: Error, Equatable {
    case emptySelection
    case tooManyItems
    case unsupportedType
}

/// Converts one reviewed native file selection into the durable v3 queue.
/// Picker URLs are adopted as read-only originals; bytes are coordinated and
/// the complete ordinary batch is published atomically.
public final class ManualCaptureSubmission: @unchecked Sendable {
    public static let maximumItems = 10

    private let accessStore: NativeAccessStore
    private let preparation: CapturePreparationCoordinator
    private let subscriptions: FileSubscriptionStore
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
        subscriptions = environment.fileSubscriptions
        self.workspaceRef = workspaceRef
        self.targetRef = targetRef
    }

    public func submit(_ candidates: [ManualCaptureCandidate]) async throws -> [CaptureQueueEntry] {
        guard !candidates.isEmpty else { throw ManualCaptureSubmissionError.emptySelection }
        guard candidates.count <= Self.maximumItems else { throw ManualCaptureSubmissionError.tooManyItems }
        let submissionID = Self.identifier(prefix: "submission")
        let createdAt = ISO8601DateFormatter().string(from: Date())
        var items: [CapturePreparationItem] = []
        var pendingSubscriptions: [FileSubscription] = []
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
                originalBinding: OriginalBindingIntent(
                    accessRecordId: descriptor.accessRef,
                    bindingGeneration: descriptor.generation,
                    linkedDocumentId: documentID
                ),
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
