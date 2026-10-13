import Foundation

/// Names the durable native-intake record that owns a candidate, so a candidate
/// that leaves review — discarded, dropped, published — releases exactly that
/// record and the resources it names.
public struct NativeCandidateKey: Hashable, Sendable {
    public let nativeRequestID: String
    public let candidateRef: String

    public init(nativeRequestID: String, candidateRef: String) {
        self.nativeRequestID = nativeRequestID
        self.candidateRef = candidateRef
    }
}

public struct ManualCaptureCandidate: Sendable, Equatable {
    public enum Source: Sendable, Equatable {
        case original
        case capturedSnapshot
        case adoptedOriginal(
            accessRef: String,
            generation: UInt64,
            linkedDocumentID: String
        )

        public var isSnapshot: Bool {
            if case .capturedSnapshot = self { return true }
            return false
        }

        public var isAdoptedOriginal: Bool {
            if case .adoptedOriginal = self { return true }
            return false
        }
    }

    public let url: URL
    public let displayName: String
    public let surface: CaptureSurface
    public let keepUpdated: Bool
    public let source: Source
    /// Set only for a candidate adopted from an authenticated native handoff.
    public let native: NativeCandidateKey?

    public init(
        url: URL,
        displayName: String? = nil,
        surface: CaptureSurface = .manual,
        keepUpdated: Bool = false,
        source: Source = .original,
        native: NativeCandidateKey? = nil
    ) {
        self.url = url
        self.displayName = displayName ?? url.lastPathComponent
        self.surface = surface
        self.keepUpdated = keepUpdated
        self.source = source
        self.native = native
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
    private let queue: CaptureIntentQueue
    private let snapshotStore: OwnedSnapshotStore?
    /// Durable records of candidates adopted from native handoffs. Present only
    /// where a native handoff can deliver candidates.
    private let candidateStore: NativeIntakeCandidateStore?
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
        self.accessStore = environment.accessStore
        preparation = environment.preparation
        subscriptions = environment.fileSubscriptions
        queue = environment.queue
        snapshotStore = try OwnedSnapshotStore(
            root: stateRoot.appendingPathComponent("owned-snapshots", isDirectory: true)
        )
        candidateStore = nil
        binding = .companion(workspaceRef: workspaceRef, targetRef: targetRef)
    }

    public init(
        environment: DocumentIntakeEnvironment,
        workspaceRef: String,
        targetRef: String = "local",
        snapshotStore: OwnedSnapshotStore? = nil,
        candidates: NativeIntakeCandidateStore? = nil
    ) {
        accessStore = environment.accessStore
        preparation = environment.preparation
        subscriptions = environment.fileSubscriptions
        queue = environment.queue
        self.snapshotStore = snapshotStore
        candidateStore = candidates
        binding = .companion(workspaceRef: workspaceRef, targetRef: targetRef)
    }

    public init(
        environment: DocumentIntakeEnvironment,
        binding: CaptureDeliveryBinding,
        snapshotStore: OwnedSnapshotStore? = nil,
        candidates: NativeIntakeCandidateStore? = nil
    ) {
        accessStore = environment.accessStore
        preparation = environment.preparation
        subscriptions = environment.fileSubscriptions
        queue = environment.queue
        self.snapshotStore = snapshotStore
        candidateStore = candidates
        self.binding = binding
    }

    public func submit(_ candidates: [ManualCaptureCandidate]) async throws -> [CaptureQueueEntry] {
        guard !candidates.isEmpty else { throw ManualCaptureSubmissionError.emptySelection }
        guard candidates.count <= Self.maximumItems else { throw ManualCaptureSubmissionError.tooManyItems }
        let automaticCandidates = candidates.filter(\.keepUpdated)
        guard !automaticCandidates.contains(where: { $0.source.isSnapshot }) else {
            throw ManualCaptureSubmissionError.snapshotCannotBeMonitored
        }
        let existingAutomatic = subscriptions.all().filter { $0.state != .stopped }
        guard existingAutomatic.count + automaticCandidates.count <= Self.maximumAutomaticFiles else {
            throw ManualCaptureSubmissionError.automaticSourceLimitExceeded
        }
        let existingPaths = Set<String>(existingAutomatic.compactMap { subscription -> String? in
            guard subscription.targetRef == binding.targetRef,
                  subscription.workspaceRef == binding.workspaceRef
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
            let documentID: String
            let descriptor: NativeAccessDescriptor
            switch candidate.source {
            case let .adoptedOriginal(accessRef, generation, linkedDocumentID):
                documentID = linkedDocumentID
                guard let existing = accessStore.descriptors().first(where: {
                    $0.accessRef == accessRef
                        && $0.generation == generation
                        && $0.linkedDocumentID == linkedDocumentID
                        && $0.state == .connected
                }) else { throw NativeAccessError.unknownAccess }
                descriptor = existing
            case .original, .capturedSnapshot:
                documentID = Self.identifier(prefix: "doc")
                descriptor = try accessStore.adopt(
                    accessRef: Self.identifier(prefix: "access"),
                    linkedDocumentID: documentID,
                    selectedURL: candidate.url,
                    route: .directFile
                )
            }
            if candidate.source.isSnapshot { snapshotAccessRefs.append(descriptor.accessRef) }
            let seed = CaptureIntentSeed(
                submissionId: submissionID,
                itemId: itemID,
                targetRef: binding.targetRef,
                workspaceRef: binding.workspaceRef,
                targetBindingRevision: binding.targetBindingRevision,
                callerContextBinding: binding.callerContextBinding,
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
                originalBinding: candidate.source.isSnapshot
                    ? nil
                    : OriginalBindingIntent(
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
                    targetRef: binding.targetRef,
                    workspaceRef: binding.workspaceRef,
                    targetBindingRevision: binding.targetBindingRevision,
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
        snapshotStore?.remove(candidates.filter { $0.source.isSnapshot }.map(\.url))
        // Ownership moved to the queue: its intents now hold the originals'
        // grants, and the snapshots' bytes were just consumed. Only the
        // candidate records are released here; no grant is disconnected.
        releaseRecords(of: candidates)
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

    /// Converts a live implicit handoff URL into a companion-owned persistent
    /// read-only grant before the extension is acknowledged. A caller that owns
    /// a durable adoption record supplies its reserved identities so a replay of
    /// the same request can recognise the grant it already created.
    public func adoptOriginal(
        _ url: URL,
        displayName: String,
        surface: CaptureSurface,
        accessRef: String? = nil,
        linkedDocumentID: String? = nil
    ) throws -> ManualCaptureCandidate {
        guard Self.contentKind(for: url) != nil else {
            throw ManualCaptureSubmissionError.unsupportedType
        }
        let descriptor = try accessStore.adopt(
            accessRef: accessRef ?? Self.identifier(prefix: "candidate-access"),
            linkedDocumentID: linkedDocumentID ?? Self.identifier(prefix: "candidate-doc"),
            selectedURL: url,
            route: .directFile
        )
        return ManualCaptureCandidate(
            url: url,
            displayName: displayName,
            surface: surface,
            source: .adoptedOriginal(
                accessRef: descriptor.accessRef,
                generation: descriptor.generation,
                linkedDocumentID: descriptor.linkedDocumentID
            )
        )
    }

    /// Rebuilds the candidate for a grant this companion already owns, reading
    /// the file through its own persistent bookmark. Returns nil when no
    /// connected record holds that identity, so the caller adopts it once.
    public func adoptedCandidate(
        accessRef: String,
        linkedDocumentID: String,
        displayName: String,
        surface: CaptureSurface
    ) throws -> ManualCaptureCandidate? {
        guard let descriptor = accessStore.descriptors().first(where: {
            $0.accessRef == accessRef
                && $0.linkedDocumentID == linkedDocumentID
                && $0.route == .directFile
                && $0.state == .connected
        }) else { return nil }
        let url = try accessStore.withReadAccess(
            accessRef: descriptor.accessRef,
            expectedGeneration: descriptor.generation
        ) { $0 }
        return ManualCaptureCandidate(
            url: url,
            displayName: displayName,
            surface: surface,
            source: .adoptedOriginal(
                accessRef: descriptor.accessRef,
                generation: descriptor.generation,
                linkedDocumentID: descriptor.linkedDocumentID
            )
        )
    }

    /// Releases candidates that leave review without being published. Their
    /// access and owned bytes go first, then their durable records: a crash
    /// between the two leaves an adopted record whose resources are gone, which
    /// recovery releases, where the reverse order could leave a released record
    /// that still holds access.
    public func discardUnsubmitted(_ candidates: [ManualCaptureCandidate]) {
        var snapshots: [URL] = []
        for candidate in candidates {
            switch candidate.source {
            case let .adoptedOriginal(accessRef, _, _): releaseGrant(accessRef: accessRef)
            case .capturedSnapshot: snapshots.append(candidate.url)
            case .original: break
            }
        }
        snapshotStore?.remove(snapshots)
        releaseRecords(of: candidates)
    }

    /// Disconnects a grant this companion adopted for a candidate, unless
    /// submitted work still names it. A queued capture or an active file
    /// subscription owns the grant it is bound to, so every release path —
    /// discard, rollback, orphan recovery — is safe to call for a candidate
    /// that was in fact published.
    public func releaseGrant(accessRef: String) {
        guard !isBound(accessRef: accessRef) else { return }
        _ = try? accessStore.disconnect(accessRef: accessRef)
    }

    /// True when a queued capture or an active file subscription is bound to
    /// this grant, which means its candidate was published.
    public func isBound(accessRef: String) -> Bool {
        queue.allEntries().contains { $0.intent.originalBinding?.accessRecordId == accessRef }
            || subscriptions.all().contains { $0.accessRef == accessRef && $0.state != .stopped }
    }

    private func releaseRecords(of candidates: [ManualCaptureCandidate]) {
        guard let candidateStore else { return }
        let byRequest = Dictionary(grouping: candidates.compactMap(\.native), by: \.nativeRequestID)
        for (request, keys) in byRequest {
            // Failing to record a release must not undo work already done: a
            // publication cannot be taken back and a discard has already
            // dropped the resources. The record then stays adopted, and on the
            // next start recovery finds its resources gone, or its grant bound
            // to a queued capture, and releases it without showing it again.
            try? candidateStore.release(
                nativeRequestID: request,
                candidateRefs: Set(keys.map(\.candidateRef))
            )
        }
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
