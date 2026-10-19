import CryptoKit
import Foundation

public enum FolderCaptureMode: String, Codable, Sendable {
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

/// One reviewed folder scope. Every count the review displays comes from here,
/// and `limit` being non-nil means the scan hit a bound: the first subset is
/// never accepted as the whole folder.
public struct FolderCapturePlan: Equatable, Sendable {
    public let root: URL
    public let rootDisplayName: String
    public let destinationWorkspaceRef: String
    public let policy: FolderCapturePolicy
    public let members: [FolderCaptureMember]
    /// The exact versioned policies frozen into this plan's consent.
    public let approvedFormatPolicyRef: String
    public let exclusionsPolicyRef: String
    /// Per-reason counts, so review can show why items were left out.
    public let exclusionCounts: [FolderExclusionReason: Int]
    public let examinedEntries: Int
    public let limit: FolderScopeLimitBreach?
    /// Parts of the scope the traversal could not read. Non-empty means this
    /// membership is not the folder, so submission is refused.
    public let coverageFailures: [FolderScopeCoverageFailure]

    /// Nil when coverage is complete. Never names a path.
    public var coverageDetail: String? {
        FolderScopeCoverageFailure.reviewDetail(coverageFailures)
    }

    /// Sum of the sizes actually read. Items whose size could not be read are
    /// counted in `skippedUnavailable` instead of guessed at.
    public var knownBytes: UInt64 { members.reduce(0) { $0 + $1.byteLength } }
    public var totalBytes: UInt64 { knownBytes }

    private func count(_ bucket: FolderScopeBucket) -> Int {
        exclusionCounts.reduce(0) { $0 + ($1.key.bucket == bucket ? $1.value : 0) }
    }

    public var skippedHidden: Int { exclusionCounts[.hidden] ?? 0 }
    public var skippedExcluded: Int { count(.excluded) - skippedHidden }
    public var skippedUnsupported: Int { count(.unsupported) }
    public var skippedUnavailable: Int { count(.unavailable) }

    /// The reviewed scope line. Unknown sizes are reported as unavailable
    /// rather than folded into a total that would then be wrong.
    public var scopeDetail: String {
        let size = ByteCountFormatter.string(fromByteCount: Int64(knownBytes), countStyle: .file)
        return "\(members.count) supported files · \(size) · "
            + "\(skippedExcluded + skippedHidden) excluded · "
            + "\(skippedUnsupported) unsupported · \(skippedUnavailable) unavailable"
    }
}

public enum FolderCaptureSubmissionError: Error, Equatable {
    case rootUnavailable
    case emptyScope
    case unsafeMember
    case unsafeRoot
    case scanLimitExceeded
    case coverageIncomplete
    case duplicateWatch
    case watchLimitExceeded
    /// A durable run or subscription already exists under this intent's identity
    /// but is not the work this intent describes. Establishment stops rather
    /// than reconciling two different plans onto one grant.
    case establishmentConflict
}

/// What one folder submission durably became: the submission identity every
/// queued child carries, and the children published so far. The identity is nil
/// only when nothing durable was created (an import-once scope with no member).
public struct FolderCaptureReceipt: Equatable, Sendable {
    public let submissionID: String?
    public let entries: [CaptureQueueEntry]
}

/// Reviews and freezes one selected folder without traversing symlinks or packages.
/// Import-once and the initial Keep Updated baseline share the durable v3 queue;
/// subsequent observations use the separately persisted subscription scanner.
public final class FolderCaptureSubmission: @unchecked Sendable {
    public static let maximumMembers = FolderScopeLimits.standard.maximumEligibleFiles
    public static let maximumExaminedEntries = FolderScopeLimits.standard.maximumExaminedEntries
    public static let maximumRecursionDepth = FolderScopeLimits.standard.maximumDepth
    public static let maximumActiveWatches = 20

    /// Kept so a pending establishment can be resumed under the binding its own
    /// intent recorded rather than this submission's.
    private let environment: DocumentIntakeEnvironment
    private let accessStore: NativeAccessStore
    private let reader: any CoordinatedOriginalReading
    private let subscriptions: FolderSubscriptionStore
    private let bulkRuns: FolderBulkRunStore
    private let bulkCoordinator: FolderBulkRunCoordinator
    private let canonicalSources: CanonicalSourceRegistry
    /// One source of truth for the target, workspace and caller facts frozen
    /// into every intent this submission queues; delivery requires an exact
    /// match against the transport's binding.
    private let binding: CaptureDeliveryBinding
    /// Canonical Core/native-owned stores, excluded from every scope so no scan
    /// can ingest the companion's own state. Not a user toggle.
    private let controlledStores: [URL]
    /// Injectable so the decision table can be proven without materialising ten
    /// thousand files. Production always uses `.standard`.
    private let limits: FolderScopeLimits

    /// Test seams for each point establishment can be interrupted at. Throwing
    /// from one is a crash at exactly that point: whatever is already durable
    /// stays durable, and nothing after it runs. Production leaves them nil.
    var afterIntentRecorded: (() throws -> Void)?
    var afterMemberEstablished: ((Int) throws -> Void)?
    var afterSubscriptionRegistered: (() throws -> Void)?
    var afterRunRegistered: (() throws -> Void)?
    var beforeActivation: (() throws -> Void)?
    var afterActivation: (() throws -> Void)?

    public var destinationWorkspaceRef: String { binding.workspaceRef }

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
        self.environment = environment
        accessStore = environment.accessStore
        self.reader = environment.reader
        subscriptions = environment.folderSubscriptions
        bulkRuns = environment.folderBulkRuns
        bulkCoordinator = environment.folderBulkCoordinator
        canonicalSources = environment.canonicalSources
        binding = .companion(workspaceRef: workspaceRef, targetRef: targetRef)
        controlledStores = environment.controlledStores
        limits = .standard
    }

    public init(
        environment: DocumentIntakeEnvironment,
        workspaceRef: String,
        targetRef: String = "local",
        limits: FolderScopeLimits = .standard
    ) {
        self.environment = environment
        accessStore = environment.accessStore
        reader = environment.reader
        subscriptions = environment.folderSubscriptions
        bulkRuns = environment.folderBulkRuns
        bulkCoordinator = environment.folderBulkCoordinator
        canonicalSources = environment.canonicalSources
        binding = .companion(workspaceRef: workspaceRef, targetRef: targetRef)
        controlledStores = environment.controlledStores
        self.limits = limits
    }

    public init(
        environment: DocumentIntakeEnvironment,
        binding: CaptureDeliveryBinding,
        limits: FolderScopeLimits = .standard
    ) {
        self.environment = environment
        accessStore = environment.accessStore
        reader = environment.reader
        subscriptions = environment.folderSubscriptions
        bulkRuns = environment.folderBulkRuns
        bulkCoordinator = environment.folderBulkCoordinator
        canonicalSources = environment.canonicalSources
        self.binding = binding
        controlledStores = environment.controlledStores
        self.limits = limits
    }

    /// Reviews one selected root with the visible recursion/exclusion/format
    /// policy. This reads directory metadata only: no byte is captured, no
    /// evidence is created and no watcher is registered.
    public func plan(root: URL, policy: FolderCapturePolicy) throws -> FolderCapturePlan {
        let rootValues = try root.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
        guard root.isFileURL, rootValues.isDirectory == true, rootValues.isSymbolicLink != true else {
            throw FolderCaptureSubmissionError.rootUnavailable
        }
        let canonicalRoot = root.resolvingSymlinksInPath().standardizedFileURL
        guard !FolderScopePolicy.isRefusedRoot(canonicalRoot, controlledStores: controlledStores) else {
            throw FolderCaptureSubmissionError.unsafeRoot
        }
        let scan: FolderScopeScan
        do {
            scan = try FolderScopePolicy.scan(
                root: canonicalRoot,
                recursive: policy.recursive,
                includeHidden: policy.includeHidden,
                controlledStores: controlledStores,
                limits: limits
            )
        } catch FolderScopeScanError.unsafeMember {
            throw FolderCaptureSubmissionError.unsafeMember
        } catch {
            throw FolderCaptureSubmissionError.rootUnavailable
        }
        // Only a scan that saw the whole folder may report it as empty.
        if scan.isComplete, scan.members.isEmpty,
           !(policy.mode == .keepUpdated && policy.watchInitialContent == .onlyNewAndChanged)
        {
            throw FolderCaptureSubmissionError.emptyScope
        }
        return FolderCapturePlan(
            root: canonicalRoot,
            rootDisplayName: canonicalRoot.lastPathComponent,
            destinationWorkspaceRef: binding.workspaceRef,
            policy: policy,
            members: scan.members.map {
                FolderCaptureMember(
                    url: $0.url,
                    relativeComponents: $0.relativeComponents,
                    displayName: $0.displayName,
                    contentKind: $0.contentKind,
                    byteLength: $0.byteLength
                )
            },
            approvedFormatPolicyRef: FolderScopePolicy.approvedFormatPolicyRef,
            exclusionsPolicyRef: FolderScopePolicy.exclusionsPolicyRef,
            exclusionCounts: scan.exclusionCounts,
            examinedEntries: scan.examinedEntries,
            limit: scan.limit,
            coverageFailures: scan.coverageFailures
        )
    }

    /// Freezes the reviewed scope into a durable plan. A scan that hit a bound
    /// enrols nothing: narrowing the scope is the user's explicit next step, so
    /// there is no silent partial enrolment of whichever subset was scanned
    /// first.
    public func submit(_ plan: FolderCapturePlan) async throws -> [CaptureQueueEntry] {
        try await submitWithReceipt(plan).entries
    }

    /// `submit`, plus the identity of the submission it created, so the caller
    /// can name this folder's own receipt.
    public func submitWithReceipt(_ plan: FolderCapturePlan) async throws -> FolderCaptureReceipt {
        // Same rule for a scope that could not be read as for one that hit a
        // bound: whatever subset was visible is not the folder, so it enrols
        // nothing and no watch is established over it.
        if !plan.coverageFailures.isEmpty {
            throw FolderCaptureSubmissionError.coverageIncomplete
        }
        if plan.limit != nil { throw FolderCaptureSubmissionError.scanLimitExceeded }
        guard plan.approvedFormatPolicyRef == FolderScopePolicy.approvedFormatPolicyRef,
              plan.exclusionsPolicyRef == FolderScopePolicy.exclusionsPolicyRef,
              plan.destinationWorkspaceRef == binding.workspaceRef
        else { throw FolderCaptureSubmissionError.unsafeRoot }
        // An establishment an earlier attempt left pending owns authority this
        // one would otherwise count as absent, so it is finished first: the
        // watch limit and the duplicate-watch check below then see every folder
        // that is really enrolled.
        await Self.reconcilePending(environment: environment)
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
                return (try? accessStore.withRootReadAccess(
                    rootRef: subscription.rootRef,
                    expectedGeneration: subscription.rootBindingGeneration
                ) { root in
                    root.resolvingSymlinksInPath().standardizedFileURL.path == requestedPath
                }) == true
            }
            guard !duplicate else { throw FolderCaptureSubmissionError.duplicateWatch }
        }
        let keepsUpdated = plan.policy.mode == .keepUpdated
        let importsExisting = !keepsUpdated
            || plan.policy.watchInitialContent != .onlyNewAndChanged
        let hasBulkRun = importsExisting && !plan.members.isEmpty
        if !keepsUpdated && !hasBulkRun {
            return FolderCaptureReceipt(submissionID: nil, entries: [])
        }
        // The logical identities come from the workspace's canonical source
        // registry, so a file another root or a direct route already enrolled
        // keeps the one source and linked document it already has. A file macOS
        // will not identify, and a second path to one file inside this one
        // reviewed scope, each get identities of their own: every member of one
        // submission must carry its own durable row, grant and queued item.
        var claimed: Set<String> = []
        let canonical = try plan.members.map { member -> CanonicalSourceIdentities in
            let identities = try canonicalSources.resolve(
                url: member.url,
                workspaceRef: binding.workspaceRef,
                claimedNativeKeys: claimed
            )
            if let key = identities.nativeKey { claimed.insert(key) }
            return identities
        }
        // Every other identity this submission will ever use is minted here,
        // once, and frozen into the intent before any authority exists. A retry
        // or a restart reconciles that same intent: it never mints a
        // replacement.
        let intent = PendingFolderSubmission(
            submissionID: Self.identifier(prefix: "submission"),
            rootRef: Self.identifier(prefix: "root"),
            rootDisplayName: plan.root.lastPathComponent,
            runID: hasBulkRun ? Self.identifier(prefix: "bulk") : nil,
            manifestRevision: Self.identifier(prefix: "manifest"),
            subscriptionID: keepsUpdated ? Self.identifier(prefix: "subscription") : nil,
            observationEpoch: Self.identifier(prefix: "observation"),
            binding: NativeRootAccessBinding(binding),
            authority: keepsUpdated ? .watchAndMembers : .importPlan,
            mode: plan.policy.mode,
            recursive: plan.policy.recursive,
            includeHidden: plan.policy.includeHidden,
            watchInitialContent: plan.policy.watchInitialContent,
            approvedFormatPolicyRef: plan.approvedFormatPolicyRef,
            exclusionsPolicyRef: plan.exclusionsPolicyRef,
            configGeneration: 1,
            consentGeneration: 1,
            createdAt: ISO8601DateFormatter().string(from: Date()),
            members: zip(plan.members, canonical).map { member, identities in
                PendingFolderMember(
                    memberID: Self.identifier(prefix: "member"),
                    accessRef: Self.identifier(prefix: "access"),
                    linkedDocumentID: identities.linkedDocumentID,
                    requestedSourceIdentity: identities.requestedSourceIdentity,
                    relativeComponents: member.relativeComponents,
                    displayName: member.displayName,
                    contentKind: member.contentKind,
                    itemID: Self.identifier(prefix: "item"),
                    payloadRef: Self.identifier(prefix: "payload"),
                    operationKey: Self.identifier(prefix: "capture"),
                    declaredByteLength: member.byteLength
                )
            }
        )
        let entries = try await establish(intent, selectedRoot: plan.root)
        return FolderCaptureReceipt(submissionID: intent.submissionID, entries: entries)
    }

    /// Finishes every establishment a crash or a failed submission left pending,
    /// each under the binding its own intent recorded. An intent that still
    /// cannot continue stays pending rather than being discarded, so the next
    /// attempt reconciles it again.
    @discardableResult
    public static func reconcilePending(
        environment: DocumentIntakeEnvironment
    ) async -> [String] {
        var finished: [String] = []
        for intent in environment.accessStore.pendingFolderEstablishments() {
            let submission = FolderCaptureSubmission(
                environment: environment,
                binding: CaptureDeliveryBinding(intent.binding)
            )
            if (try? await submission.establish(intent)) != nil {
                finished.append(intent.submissionID)
            }
        }
        return finished
    }

    /// Carries one frozen intent from its durable record to established work.
    ///
    /// Every step is idempotent for exactly this intent and only for it, so an
    /// interrupted establishment resumes here instead of starting a second one:
    /// no identity is regenerated, no member is added, a durable record that
    /// already exists is verified rather than rewritten, and the root is
    /// activated — which is what lets anything discover through it — only once
    /// the matching subscription or run record exists. The intent is retired
    /// last, so a lost completion is replayed as a no-op rather than lost work.
    func establish(
        _ intent: PendingFolderSubmission,
        selectedRoot: URL? = nil
    ) async throws -> [CaptureQueueEntry] {
        let root = try accessStore.beginFolderEstablishment(intent, selectedURL: selectedRoot)
        try afterIntentRecorded?()
        let establishedSubscription = intent.subscriptionID
            .flatMap(subscriptions.subscription(id:))
        var runMembers: [FolderBulkRunMember] = []
        var subscriptionMembers: [FolderSubscriptionMember] = []
        for (index, member) in intent.members.enumerated() {
            let descriptor = try accessStore.adoptRootMember(
                accessRef: member.accessRef,
                linkedDocumentID: member.linkedDocumentID,
                rootRef: intent.rootRef,
                memberRef: member.memberID,
                relativeComponents: member.relativeComponents,
                expectedRootGeneration: root.generation
            )
            runMembers.append(
                FolderBulkRunMember(
                    memberID: member.memberID,
                    accessRef: descriptor.accessRef,
                    linkedDocumentID: member.linkedDocumentID,
                    requestedSourceIdentity: member.requestedSourceIdentity,
                    relativeComponents: member.relativeComponents,
                    displayName: member.displayName,
                    contentKind: member.contentKind,
                    bindingGeneration: descriptor.generation,
                    itemID: member.itemID,
                    payloadRef: member.payloadRef,
                    operationKey: member.operationKey,
                    declaredByteLength: member.declaredByteLength,
                    state: .planned,
                    failureCode: nil
                )
            )
            if intent.subscriptionID != nil {
                // Only an establishment that still has to write the subscription
                // reads a baseline. Replaying one that exists keeps the
                // observations already recorded against it: a second reading
                // would be a different, later observation under the same frozen
                // consent. The member itself is still rebuilt, because that is
                // what the durable record is verified against.
                let excludedBaseline = intent.watchInitialContent == .onlyNewAndChanged
                let baselineBytes = excludedBaseline && establishedSubscription == nil
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
                        memberID: member.memberID,
                        accessRef: descriptor.accessRef,
                        linkedDocumentID: member.linkedDocumentID,
                        requestedSourceIdentity: member.requestedSourceIdentity,
                        relativeComponents: member.relativeComponents,
                        displayName: member.displayName,
                        contentKind: member.contentKind,
                        membershipGeneration: descriptor.generation,
                        baselineItemID: excludedBaseline ? nil : member.itemID,
                        currentSuccessfulCaptureID: nil,
                        lastObservedDigest: baselineBytes.map(Self.digest),
                        lastObservedByteLength: baselineBytes.map { UInt64($0.count) },
                        lastObservedAt: excludedBaseline ? intent.createdAt : nil,
                        lastQueuedItemID: excludedBaseline ? nil : member.itemID,
                        dirtyEpoch: 1,
                        state: .present
                    )
                )
            }
            try afterMemberEstablished?(index)
        }
        // Membership is frozen here, at approval — not at completion. A sibling
        // created after this point is outside the group for good, so it never
        // becomes readable or capturable through it, including after a restart.
        if intent.subscriptionID == nil {
            _ = try accessStore.freezeRootMembers(
                rootRef: intent.rootRef,
                memberRefs: runMembers.map(\.memberID)
            )
        }
        if let subscriptionID = intent.subscriptionID {
            // The exact record this establishment would write, which is also
            // what an already-written one is verified against.
            let planned = FolderSubscription(
                subscriptionID: subscriptionID,
                rootRef: intent.rootRef,
                rootBindingGeneration: root.generation,
                rootDisplayName: intent.rootDisplayName,
                targetRef: intent.binding.targetRef,
                workspaceRef: intent.binding.workspaceRef,
                targetBindingRevision: intent.binding.targetBindingRevision,
                callerContextBinding: intent.binding.callerContextBinding,
                recursive: intent.recursive,
                includeHidden: intent.includeHidden,
                configGeneration: intent.configGeneration,
                consentGeneration: intent.consentGeneration,
                consentRecordedAt: intent.createdAt,
                observationEpoch: intent.observationEpoch,
                state: intent.watchInitialContent == .onlyNewAndChanged
                    ? .active : .waitingForCore,
                needsFullReconciliation: true,
                lastCompletedMetadataScanAt: nil,
                pendingReviewSignature: nil,
                approvedReviewSignature: nil,
                reviewChangeCount: nil,
                reviewExamples: nil,
                members: subscriptionMembers
            )
            if let establishedSubscription {
                try Self.verify(establishedSubscription, matches: planned)
            } else {
                _ = try subscriptions.register(planned)
            }
            try afterSubscriptionRegistered?()
        }
        if let runID = intent.runID {
            let planned = FolderBulkRun(
                runID: runID,
                manifestRevision: intent.manifestRevision,
                submissionID: intent.submissionID,
                rootRef: intent.rootRef,
                rootBindingGeneration: root.generation,
                rootDisplayName: intent.rootDisplayName,
                subscriptionID: intent.subscriptionID,
                targetRef: intent.binding.targetRef,
                workspaceRef: intent.binding.workspaceRef,
                targetBindingRevision: intent.binding.targetBindingRevision,
                callerContextBinding: intent.binding.callerContextBinding,
                approvedFormatPolicyRef: intent.approvedFormatPolicyRef,
                exclusionsPolicyRef: intent.exclusionsPolicyRef,
                createdAt: intent.createdAt,
                state: .adding,
                members: runMembers
            )
            if let established = bulkRuns.run(id: runID) {
                try Self.verify(established, matches: planned)
            } else {
                _ = try bulkRuns.register(planned)
            }
            try afterRunRegistered?()
        }
        try beforeActivation?()
        // A durable owner's record now exists, so the root is no longer merely
        // prepared: it is established work, and may be discovered through.
        _ = try accessStore.activateRoot(
            rootRef: intent.rootRef,
            expectedGeneration: root.generation
        )
        try afterActivation?()
        try accessStore.completeFolderEstablishment(submissionID: intent.submissionID)
        guard let runID = intent.runID else { return [] }
        return await bulkCoordinator.advance(runID: runID).newlyPublished
    }

    /// A record under this intent's identity must be this intent's work.
    ///
    /// The durable record is compared against the one this establishment would
    /// have written, so every immutable field is covered without a list that
    /// can fall behind the type: the root and its binding generation, the
    /// manifest revision, the caller binding and target binding revision, the
    /// reviewed policy refs, the root display name, the creation time, and each
    /// member's locator, every frozen identity it carries, its display name,
    /// content kind and declared length. A member's own progress and the run's
    /// lifecycle state are exactly what a replay must leave alone, so they are
    /// taken from what is already durable.
    private static func verify(_ established: FolderBulkRun, matches expected: FolderBulkRun) throws {
        // A run's membership is immutable, so the two lists are the same
        // members in the same order or this is not the same manifest.
        guard established.members.count == expected.members.count else {
            throw FolderCaptureSubmissionError.establishmentConflict
        }
        var rebased = expected
        rebased.state = established.state
        rebased.members = zip(expected.members, established.members).map { planned, durable in
            var member = planned
            member.state = durable.state
            member.failureCode = durable.failureCode
            return member
        }
        guard rebased == established else {
            throw FolderCaptureSubmissionError.establishmentConflict
        }
    }

    /// The same rule for a watch. Observations, review state and the members a
    /// live watch has discovered since are progress and are kept as they are,
    /// but every member this intent froze must still be there with every
    /// immutable field — locator, identities, display name, content kind,
    /// membership generation and baseline item — exactly as the intent
    /// describes it.
    private static func verify(
        _ established: FolderSubscription,
        matches expected: FolderSubscription
    ) throws {
        var rebased = expected
        rebased.state = established.state
        rebased.needsFullReconciliation = established.needsFullReconciliation
        rebased.lastCompletedMetadataScanAt = established.lastCompletedMetadataScanAt
        rebased.pendingReviewSignature = established.pendingReviewSignature
        rebased.approvedReviewSignature = established.approvedReviewSignature
        rebased.reviewChangeCount = established.reviewChangeCount
        rebased.reviewExamples = established.reviewExamples
        // Membership is checked per reviewed member below, because a watch may
        // legitimately have enrolled more since it was activated.
        rebased.members = established.members
        guard rebased == established else {
            throw FolderCaptureSubmissionError.establishmentConflict
        }
        let durable = Dictionary(
            established.members.map { ($0.memberID, $0) },
            uniquingKeysWith: { first, _ in first }
        )
        for planned in expected.members {
            guard let recorded = durable[planned.memberID] else {
                throw FolderCaptureSubmissionError.establishmentConflict
            }
            var member = planned
            member.currentSuccessfulCaptureID = recorded.currentSuccessfulCaptureID
            member.lastObservedDigest = recorded.lastObservedDigest
            member.lastObservedByteLength = recorded.lastObservedByteLength
            member.lastObservedAt = recorded.lastObservedAt
            member.lastQueuedItemID = recorded.lastQueuedItemID
            member.dirtyEpoch = recorded.dirtyEpoch
            member.state = recorded.state
            guard member == recorded else {
                throw FolderCaptureSubmissionError.establishmentConflict
            }
        }
    }

    private static func identifier(prefix: String) -> String {
        "\(prefix)-\(UUID().uuidString.lowercased())"
    }

    private static func digest(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
