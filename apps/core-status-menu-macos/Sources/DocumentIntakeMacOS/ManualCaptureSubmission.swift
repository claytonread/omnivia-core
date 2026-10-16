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
        /// A provisional companion-owned root grant on one selected folder.
        /// It publishes nothing on its own: the folder's scope is reviewed
        /// separately, and the work that submission creates adopts its own
        /// root grant under its own owner.
        case adoptedFolderRoot(
            rootRef: String,
            generation: UInt64,
            ownerRef: String
        )

        public var isSnapshot: Bool {
            if case .capturedSnapshot = self { return true }
            return false
        }

        public var isAdoptedOriginal: Bool {
            if case .adoptedOriginal = self { return true }
            return false
        }

        public var isAdoptedFolderRoot: Bool {
            if case .adoptedFolderRoot = self { return true }
            return false
        }

        /// The generation of the root grant this source holds, when it holds one.
        public var adoptedRootGeneration: UInt64? {
            if case let .adoptedFolderRoot(_, generation, _) = self { return generation }
            return nil
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
    private let canonicalSources: CanonicalSourceRegistry

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
        canonicalSources = environment.canonicalSources
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
        canonicalSources = environment.canonicalSources
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
        canonicalSources = environment.canonicalSources
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
        var adoptedAccessRefs: [String] = []
        defer {
            // Every grant this submission adopted goes unless published work now
            // owns it, so a batch that was refused or failed leaves none behind
            // and a retry of the same selection cannot pile them up. A snapshot's
            // grant is owned by nothing, so it always goes.
            for accessRef in adoptedAccessRefs { releaseGrant(accessRef: accessRef) }
        }
        for candidate in candidates {
            guard let kind = Self.contentKind(for: candidate.url) else {
                throw ManualCaptureSubmissionError.unsupportedType
            }
            let itemID = Self.identifier(prefix: "item")
            let documentID: String
            let descriptor: NativeAccessDescriptor
            var canonical: CanonicalSourceIdentities?
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
            case .adoptedFolderRoot:
                // A folder is published by folder submission, which adopts its
                // own root grant under its own owner. This path publishes
                // documents, so it refuses a root grant rather than binding one
                // as if it were a direct file.
                throw ManualCaptureSubmissionError.unsupportedType
            case .original, .capturedSnapshot:
                // A direct route over an original takes the workspace's
                // canonical identities for that native file, so a file a
                // watched folder already enrolled keeps the one source and
                // linked document it has. A snapshot is not the original — it
                // is a copy this companion owns — so it is never canonicalised.
                canonical = candidate.source.isSnapshot
                    ? nil
                    : try canonicalSources.resolve(
                        url: candidate.url,
                        workspaceRef: binding.workspaceRef
                    )
                documentID = canonical?.linkedDocumentID ?? Self.identifier(prefix: "doc")
                descriptor = try accessStore.adopt(
                    accessRef: Self.identifier(prefix: "access"),
                    linkedDocumentID: documentID,
                    selectedURL: candidate.url,
                    route: .directFile
                )
                adoptedAccessRefs.append(descriptor.accessRef)
            }
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
                requestedSourceIdentity: canonical?.requestedSourceIdentity
                    ?? Self.identifier(prefix: "source"),
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
                pendingSubscriptions.append(subscription)
            }
        }
        // Each watch is registered inside the publication critical section,
        // once the batch is committed: a preparation that fails must not leave
        // a subscription saying this Mac is keeping a file updated that was
        // never added. The baseline facts come from the entry that proves what
        // was frozen, so a watch is never live without them.
        let watches = pendingSubscriptions
        // A candidate whose canonical source already held an initial capture of
        // exactly these bytes queued none of its own; its watch's baseline is
        // that capture, which is the one this source has.
        let coalesced = CoalescedItems()
        let entries = try await preparation.prepareAndPublish(
            items,
            binding: binding,
            onCoalesced: coalesced.record
        ) { published in
            let served = coalesced.all()
            let byItem = Dictionary(
                uniqueKeysWithValues: (published + coalesced.entries())
                    .map { ($0.intent.itemId, $0) }
            )
            for var watch in watches {
                if let existing = served[watch.baselineItemID] {
                    watch.baselineItemID = existing
                    watch.lastQueuedItemID = existing
                }
                if let entry = byItem[watch.baselineItemID] {
                    watch.lastObservedDigest = entry.intent.localIntegrityDigest
                    watch.lastObservedByteLength = entry.intent.byteLength
                    watch.lastObservedAt = entry.intent.createdAt
                }
                _ = try self.subscriptions.register(watch)
            }
        }
        snapshotStore?.remove(candidates.filter { $0.source.isSnapshot }.map(\.url))
        // Ownership moved to the queue: its intents now hold the originals'
        // grants, and the snapshots' bytes were just consumed. Only the
        // candidate records are released here; no grant is disconnected.
        releaseRecords(of: candidates)
        // A coalesced candidate added no capture of its own, but it did add a
        // route to one. The caller is told which capture serves it rather than
        // being told nothing happened.
        return entries + coalesced.entries()
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

    /// Converts a live implicit handoff folder URL into a companion-owned root
    /// grant before the extension is acknowledged. The folder is bound, not
    /// read: nothing inside it is listed, opened or captured here. The caller
    /// supplies the identities its durable record reserved, so a replay of the
    /// same request recognises the root grant it already created.
    public func adoptFolderRoot(
        _ url: URL,
        displayName: String,
        surface: CaptureSurface,
        rootRef: String,
        ownerRef: String
    ) throws -> ManualCaptureCandidate {
        // A draft's root group supports its own bounded scope preview and
        // nothing else: it enrols no member, so it conveys no capture or
        // discovery authority until a reviewed plan is submitted.
        let root = try accessStore.adoptRoot(
            rootRef: rootRef,
            ownerRef: ownerRef,
            selectedURL: url,
            binding: NativeRootAccessBinding(binding),
            authority: .importPlan
        )
        return ManualCaptureCandidate(
            url: url,
            displayName: displayName,
            surface: surface,
            source: .adoptedFolderRoot(
                rootRef: root.rootRef,
                generation: root.generation,
                ownerRef: ownerRef
            )
        )
    }

    /// Rebuilds the candidate for a root grant this companion already owns,
    /// resolving the folder through its own persistent bookmark. `generation`
    /// pins the exact binding the caller's record bound; passing nil accepts
    /// whichever generation is connected, which is what an interrupted
    /// establishment needs before it has bound one. Returns nil when no
    /// connected root holds that reference under that owner, so the caller
    /// adopts it once.
    public func adoptedFolderCandidate(
        rootRef: String,
        ownerRef: String,
        generation: UInt64? = nil,
        displayName: String,
        surface: CaptureSurface
    ) throws -> ManualCaptureCandidate? {
        guard let root = accessStore.rootDescriptors().first(where: { candidate in
            candidate.rootRef == rootRef
                // A draft's root stays prepared until a reviewed plan is
                // submitted, so its own replay resolves it in that state.
                && candidate.state.permitsResolution
                && candidate.ownerRefs.contains(ownerRef)
                && (generation == nil || candidate.generation == generation)
        }) else { return nil }
        let url = try accessStore.withRootReadAccess(
            rootRef: root.rootRef,
            expectedGeneration: root.generation
        ) { $0 }
        return ManualCaptureCandidate(
            url: url,
            displayName: displayName,
            surface: surface,
            source: .adoptedFolderRoot(
                rootRef: root.rootRef,
                generation: root.generation,
                ownerRef: ownerRef
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
            case let .adoptedFolderRoot(rootRef, _, ownerRef):
                releaseRootGrant(rootRef: rootRef, ownerRef: ownerRef)
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

    /// Gives up this candidate's share of a root grant. The release is owner
    /// scoped, so the grant itself survives while any other owner — a
    /// submitted bulk run, a watched-folder subscription — or any connected
    /// member still holds it. That is what makes every release path safe to
    /// call for a folder whose plan was in fact submitted: submitted work
    /// adopts its own owner, never this one.
    public func releaseRootGrant(rootRef: String, ownerRef: String) {
        _ = try? accessStore.releaseRoot(rootRef: rootRef, ownerRef: ownerRef)
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
        CaptureContentKind.approvedFolderFormat(forName: url.lastPathComponent)
    }

    private static func identifier(prefix: String) -> String {
        "\(prefix)-\(UUID().uuidString.lowercased())"
    }
}
