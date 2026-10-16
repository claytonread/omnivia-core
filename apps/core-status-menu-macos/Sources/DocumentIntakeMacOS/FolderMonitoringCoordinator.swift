import CryptoKit
import Foundation

public struct FolderMonitoringReport: Equatable, Sendable {
    public let subscriptionID: String
    public let state: FolderSubscriptionState
    public let examined: Int
    public let queued: Int
    public let unchanged: Int
    public let absent: Int
}

public enum FolderMonitoringError: Error, Equatable {
    case subscriptionNotActive
    case scopeUnavailable
    case scopeLimitExceeded
    case scopeCoverageIncomplete
}

struct MonitoredCandidate: Sendable {
    let url: URL
    let relativeComponents: [String]
    let displayName: String
    let contentKind: CaptureContentKind
    let byteLength: UInt64
}

/// Reconciles the current authorised folder state into bounded immutable capture intents.
/// Filesystem events are only an optimisation; calling `reconcileAll` after startup/wake and on
/// the periodic schedule is sufficient for correctness over the current observable state.
public actor FolderMonitoringCoordinator {
    private let accessStore: NativeAccessStore
    private let preparation: CapturePreparationCoordinator
    private let queue: CaptureIntentQueue
    private let subscriptions: FolderSubscriptionStore
    private let reader: any CoordinatedOriginalReading
    /// One logical source per workspace per native file; see
    /// `CanonicalSourceRegistry`. A newly discovered file takes its identities
    /// from here rather than minting its own.
    private let canonicalSources: CanonicalSourceRegistry
    /// Core/native-owned stores every scan excludes; see `DocumentIntakeEnvironment`.
    private let controlledStores: [URL]
    /// Wall clock for the observation timestamp and the add/change burst window.
    /// Production reads `Date`; a test moves it rather than waiting the window out.
    private let clock: @Sendable () -> Date

    /// The two unusual-change thresholds, and the rolling window the add/change
    /// one is a rate over. These are the design's values and are read nowhere else.
    static let changeBurstThreshold = 100
    static let changeBurstWindow: TimeInterval = 5 * 60
    static let disappearanceThreshold = 20
    static let disappearanceSharePercent = 10

    init(
        accessStore: NativeAccessStore,
        preparation: CapturePreparationCoordinator,
        queue: CaptureIntentQueue,
        subscriptions: FolderSubscriptionStore,
        reader: any CoordinatedOriginalReading,
        canonicalSources: CanonicalSourceRegistry,
        controlledStores: [URL] = [],
        clock: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.clock = clock
        self.accessStore = accessStore
        self.preparation = preparation
        self.queue = queue
        self.subscriptions = subscriptions
        self.reader = reader
        self.canonicalSources = canonicalSources
        self.controlledStores = controlledStores
    }

    /// Reconciles durable queue receipts first. A watch cannot become active until every reviewed
    /// baseline member has an acknowledged Core admission.
    @discardableResult
    public func reconcileAll() async -> [FolderMonitoringReport] {
        await reconcile(subscriptionIDs: nil)
    }

    @discardableResult
    public func reconcile(subscriptionIDs: Set<String>) async -> [FolderMonitoringReport] {
        await reconcile(subscriptionIDs: Optional(subscriptionIDs))
    }

    public func markDirty(subscriptionIDs: Set<String>) {
        for id in subscriptionIDs {
            _ = try? subscriptions.update(id: id) { value in
                guard value.state == .active else { return }
                value.needsFullReconciliation = true
            }
        }
    }

    public func activeObservationRoots() -> [FolderObservationRoot] {
        subscriptions.all().compactMap { subscription in
            guard subscription.state == .active else { return nil }
            return try? accessStore.withRootReadAccess(
                rootRef: subscription.rootRef,
                expectedGeneration: subscription.rootBindingGeneration,
                purpose: .memberDiscovery
            ) { root in
                FolderObservationRoot(subscriptionID: subscription.subscriptionID, url: root)
            }
        }
    }

    /// True once the subscription's own root group is established work. A
    /// repairable or unavailable root is still established: only one that never
    /// finished being established is not.
    ///
    /// Shared with `FolderReconciliationExecutor`, which gates its automatic
    /// passes on the same condition for the same reason.
    static func isEstablished(
        _ subscription: FolderSubscription,
        in accessStore: NativeAccessStore
    ) -> Bool {
        guard let root = accessStore.rootDescriptors()
            .first(where: { $0.rootRef == subscription.rootRef })
        else { return false }
        return root.state != .provisional
    }

    private func reconcile(subscriptionIDs: Set<String>?) async -> [FolderMonitoringReport] {
        var reports: [FolderMonitoringReport] = []
        for persisted in subscriptions.all() {
            if let subscriptionIDs, !subscriptionIDs.contains(persisted.subscriptionID) { continue }
            guard let subscription = try? reconcileAdmissions(persisted) else { continue }
            // A subscription whose root group is still only prepared belongs to
            // an establishment that has not finished. Its grant is not lost, so
            // it is reported as it stands and left for
            // `reconcilePendingFolderEstablishment` to finish rather than
            // scanned and marked as needing access.
            guard Self.isEstablished(subscription, in: accessStore) else {
                reports.append(
                    FolderMonitoringReport(
                        subscriptionID: subscription.subscriptionID,
                        state: subscription.state,
                        examined: 0,
                        queued: 0,
                        unchanged: 0,
                        absent: 0
                    )
                )
                continue
            }
            guard subscription.state == .active else {
                reports.append(
                    FolderMonitoringReport(
                        subscriptionID: subscription.subscriptionID,
                        state: subscription.state,
                        examined: 0,
                        queued: 0,
                        unchanged: 0,
                        absent: 0
                    )
                )
                continue
            }
            do {
                reports.append(try await reconcileActive(subscription))
            } catch {
                _ = try? subscriptions.update(id: subscription.subscriptionID) { value in
                    value.state = .needsAccess
                    value.needsFullReconciliation = true
                }
                reports.append(
                    FolderMonitoringReport(
                        subscriptionID: subscription.subscriptionID,
                        state: .needsAccess,
                        examined: 0,
                        queued: 0,
                        unchanged: 0,
                        absent: 0
                    )
                )
            }
        }
        return reports
    }

    /// Applies Core admission receipts without reading any watched scope. The companion calls this
    /// after queue delivery so pending activation can advance independently of the scan schedule.
    @discardableResult
    public func reconcileReceipts() -> [FolderSubscription] {
        subscriptions.all().compactMap { try? reconcileAdmissions($0) }
    }

    @discardableResult
    public func pause(subscriptionID: String) throws -> FolderSubscription {
        try subscriptions.update(id: subscriptionID) { value in
            guard value.state == .active || value.state == .needsAccess else {
                throw FolderMonitoringError.subscriptionNotActive
            }
            value.state = .paused
            value.needsFullReconciliation = true
        }
    }

    @discardableResult
    public func resume(subscriptionID: String) throws -> FolderSubscription {
        try subscriptions.update(id: subscriptionID) { value in
            guard value.state == .paused || value.state == .needsAccess || value.state == .reviewRequired else {
                throw FolderMonitoringError.subscriptionNotActive
            }
            if value.state == .reviewRequired {
                value.approvedReviewSignature = value.pendingReviewSignature
                value.pendingReviewSignature = nil
            }
            value.state = .active
            value.needsFullReconciliation = true
        }
    }

    @discardableResult
    public func stop(subscriptionID: String) throws -> FolderSubscription {
        let subscription = try subscriptions.update(id: subscriptionID) { value in
            value.state = .stopped
            value.needsFullReconciliation = false
        }
        // Stop watching keeps the same restricted manual member routes and ends
        // discovery: the group narrows before its owner is released, so no scan
        // can slip through in between.
        _ = try? accessStore.narrowRootAuthority(
            rootRef: subscription.rootRef,
            to: .frozenMembersOnly
        )
        _ = try accessStore.releaseRoot(
            rootRef: subscription.rootRef,
            ownerRef: subscription.subscriptionID
        )
        return subscription
    }

    /// The queued capture that is this membership's baseline.
    ///
    /// Usually the item the membership froze. A membership established over a
    /// file whose canonical source was already being captured at exactly those
    /// bytes queued no item of its own, so its frozen id names no entry; the
    /// capture that serves it is its source's own initial capture, the one
    /// claiming no earlier head. Nothing is inferred: with no such entry the
    /// baseline is simply unresolved, which leaves the watch waiting rather
    /// than wrongly active.
    private static func baseline(
        of member: FolderSubscriptionMember,
        in entries: [String: CaptureQueueEntry]
    ) -> CaptureQueueEntry? {
        guard let baselineID = member.baselineItemID else { return nil }
        if let entry = entries[baselineID] { return entry }
        return entries.values.first {
            $0.intent.requestedSourceIdentity == member.requestedSourceIdentity
                && CapturePreparationCoordinator.claimedHead(of: $0.intent) == nil
        }
    }

    private func reconcileAdmissions(_ persisted: FolderSubscription) throws -> FolderSubscription {
        // Stopping is a one-way door, including for a watch stopped while Core
        // had not yet confirmed its baseline. Whatever that confirmation turns
        // out to be, it arrives after the watch ended: it neither activates one
        // that was waiting nor sends one to review, which is a resumable state
        // and so the same reactivation by another name.
        guard persisted.state != .stopped else { return persisted }
        let entries = Dictionary(uniqueKeysWithValues: queue.allEntries().map { ($0.intent.itemId, $0) })
        return try subscriptions.update(id: persisted.subscriptionID) { value in
            var baselineResolved = true
            var baselineFailed = false
            for index in value.members.indices {
                if let baseline = Self.baseline(of: value.members[index], in: entries) {
                    if baseline.state == .admitted, let captureID = baseline.admittedCaptureID {
                        if value.state == .waitingForCore
                            || value.members[index].currentSuccessfulCaptureID == nil
                        {
                            value.members[index].currentSuccessfulCaptureID = captureID
                        }
                        if value.state == .waitingForCore
                            || value.members[index].lastObservedDigest == nil
                        {
                            value.members[index].lastObservedDigest = baseline.intent.localIntegrityDigest
                            value.members[index].lastObservedByteLength = baseline.intent.byteLength
                            value.members[index].lastObservedAt = baseline.intent.createdAt
                        }
                    } else {
                        baselineResolved = false
                        baselineFailed = baselineFailed || baseline.state == .failed
                    }
                } else if value.members[index].baselineItemID != nil {
                    baselineResolved = false
                }
                if let queuedID = value.members[index].lastQueuedItemID,
                   let queued = entries[queuedID]
                {
                    if queued.state == .admitted, let captureID = queued.admittedCaptureID {
                        value.members[index].currentSuccessfulCaptureID = captureID
                        value.members[index].lastQueuedItemID = nil
                    } else if queued.state == .failed {
                        value.members[index].lastQueuedItemID = nil
                        value.members[index].lastObservedDigest = nil
                        value.members[index].lastObservedByteLength = nil
                        value.members[index].lastObservedAt = nil
                        value.state = .reviewRequired
                        value.needsFullReconciliation = true
                    }
                }
            }
            if value.state == .waitingForCore {
                if baselineFailed {
                    value.state = .reviewRequired
                } else if baselineResolved {
                    value.state = .active
                    value.needsFullReconciliation = true
                }
            }
        }
    }

    private func reconcileActive(_ subscription: FolderSubscription) async throws -> FolderMonitoringReport {
        let expectedGeneration = subscription.rootBindingGeneration
        let candidates = try accessStore.withRootReadAccess(
            rootRef: subscription.rootRef,
            expectedGeneration: expectedGeneration,
            purpose: .memberDiscovery
        ) { root in
            try Self.scan(
                root: root,
                recursive: subscription.recursive,
                includeHidden: subscription.includeHidden,
                controlledStores: self.controlledStores
            )
        }
        guard candidates.count <= FolderCaptureSubmission.maximumMembers else {
            throw FolderMonitoringError.scopeLimitExceeded
        }
        let existingByPath = Dictionary(
            uniqueKeysWithValues: subscription.members.map { (Self.pathKey($0.relativeComponents), $0) }
        )
        let candidateKeys = Set(candidates.map { Self.pathKey($0.relativeComponents) })
        let queueEntries = queue.allEntries()
        let unresolvedEntries = queueEntries.filter {
            [.pending, .staging, .unknownOutcome].contains($0.state)
        }
        let unresolved = Set(
            unresolvedEntries
                .filter { entry in
                    entry.intent.consent?.subscriptionId == subscription.subscriptionID
                }
                .compactMap { $0.intent.originalBinding?.memberId }
        )
        // A source the person disconnected has no local route left. Its membership stays on
        // record, so a later scan never re-enrols it as a newly discovered child, and nothing is
        // read through the revoked grant: the Disconnect is what prevents rediscovery, rather
        // than the file having to leave the folder.
        let revoked = Set(
            accessStore.descriptors().filter { $0.state == .disconnected }.map(\.accessRef)
        )
        let credibleChanges = candidates.filter { candidate in
            guard let existing = existingByPath[Self.pathKey(candidate.relativeComponents)] else {
                return true
            }
            guard !revoked.contains(existing.accessRef) else { return false }
            return existing.lastObservedByteLength != candidate.byteLength
        }
        let credibleDisappearances = subscription.members.filter {
            $0.state == .present && !candidateKeys.contains(Self.pathKey($0.relativeComponents))
        }
        let now = clock()
        let observedAt = ISO8601DateFormatter().string(from: now)
        // The add/change threshold is a rate, not a running total: credible
        // observations accumulate inside one rolling five-minute window, and a
        // window that has run out starts again at this pass's own count. Changes
        // spread wider than the window therefore never add up to a burst, while
        // several quick passes inside one do. The count is that rate signal and
        // not an audit: a pass that returns for review leaves its own changes
        // undrained, so the pass after it sees them again.
        let windowStart = subscription.changeBurstWindowStartedAt.flatMap(Self.timestamp(from:))
        let windowOpen = windowStart.map {
            now >= $0 && now.timeIntervalSince($0) <= Self.changeBurstWindow
        } ?? false
        let burstCount = (windowOpen ? (subscription.changeBurstCount ?? 0) : 0)
            + credibleChanges.count
        let burstWindowStartedAt = windowOpen ? subscription.changeBurstWindowStartedAt : observedAt
        let changeBurstCrossed = burstCount >= Self.changeBurstThreshold
        // Both disappearance conditions are required, and the share is of the
        // inventory that was actually present: a member already recorded absent is
        // long gone and cannot disappear again, so counting it in the denominator
        // would only hide a real bulk loss. Neither condition is reachable without
        // complete coverage, because an incomplete scan never gets here (see `scan`).
        let presentCount = subscription.members.filter { $0.state == .present }.count
        let disappearanceThresholdCrossed = credibleDisappearances.count >= Self.disappearanceThreshold
            && credibleDisappearances.count * Self.disappearanceSharePercent >= max(1, presentCount)
        let reviewSignature = Self.reviewSignature(
            subscription: subscription,
            changes: credibleChanges,
            disappearances: credibleDisappearances
        )
        // An approval is exact: it exempts the one reviewed inventory under the
        // configuration and consent it was given for, and nothing else. A further
        // material change signs differently, so the scope returns to review with
        // the new counts rather than draining on the earlier approval.
        let thresholdCrossed = (changeBurstCrossed && !credibleChanges.isEmpty)
            || disappearanceThresholdCrossed
        if thresholdCrossed, subscription.approvedReviewSignature != reviewSignature {
            let examples = credibleChanges.map(\.displayName)
                + credibleDisappearances.map { $0.displayName + " (missing)" }
            let reviewed = try subscriptions.update(id: subscription.subscriptionID) { value in
                value.state = .reviewRequired
                value.needsFullReconciliation = true
                value.pendingReviewSignature = reviewSignature
                // A stale approval is not left standing beside the new pending
                // one: this scope's only live approval is the one a person gives
                // for what is on the screen now.
                value.approvedReviewSignature = nil
                value.reviewChangeCount = credibleChanges.count + credibleDisappearances.count
                value.reviewExamples = Array(examples.prefix(10))
                value.changeBurstWindowStartedAt = burstWindowStartedAt
                value.changeBurstCount = burstCount
            }
            return FolderMonitoringReport(
                subscriptionID: reviewed.subscriptionID,
                state: .reviewRequired,
                examined: candidates.count,
                queued: 0,
                unchanged: 0,
                absent: 0
            )
        }
        let bulkRunID = Self.identifier(prefix: "bulk")
        let manifestRevision = Self.identifier(prefix: "manifest")
        var updated = subscription
        var queued = 0
        var unchanged = 0
        var unresolvedCount = unresolvedEntries.count
        var capacityDeferred = false

        for candidate in candidates {
            let key = Self.pathKey(candidate.relativeComponents)
            if var member = existingByPath[key] {
                // See `revoked`: a disconnected source is left exactly as it stands.
                if revoked.contains(member.accessRef) { continue }
                member.state = .present
                if unresolved.contains(member.memberID) {
                    Self.replace(member, in: &updated.members)
                    continue
                }
                if unresolvedCount >= CaptureIntentQueue.maximumUnresolvedItems {
                    capacityDeferred = true
                    break
                }
                let bytes = try accessStore.withReadAccess(
                    accessRef: member.accessRef,
                    expectedGeneration: member.membershipGeneration,
                    expectedRootBindingGeneration: expectedGeneration
                ) { url in
                    try reader.readStableBytes(from: url, maximumBytes: CaptureIntentV3.maximumPayloadBytes)
                }
                let digest = Self.digest(bytes)
                member.lastObservedAt = observedAt
                member.lastObservedByteLength = UInt64(bytes.count)
                if digest == member.lastObservedDigest {
                    unchanged += 1
                    Self.replace(member, in: &updated.members)
                    continue
                }
                let observedDirtyEpoch = member.dirtyEpoch
                member.dirtyEpoch += 1
                let itemID = Self.identifier(prefix: "item")
                let seed = Self.seed(
                    subscription: subscription,
                    member: member,
                    itemID: itemID,
                    payloadRef: Self.identifier(prefix: "payload"),
                    operationKey: Self.identifier(prefix: "capture"),
                    base: .expected(member.currentSuccessfulCaptureID),
                    bulkRunID: bulkRunID,
                    manifestRevision: manifestRevision,
                    observedAt: observedAt,
                    rootBindingGeneration: expectedGeneration
                )
                let entries: [CaptureQueueEntry]
                let coalesced = CoalescedItems()
                do {
                    entries = try await preparation.prepareAndPublish(
                        [
                            CapturePreparationItem(
                                access: NativeAccessExpectation(
                                    accessRef: member.accessRef,
                                    generation: member.membershipGeneration,
                                    rootBindingGeneration: expectedGeneration
                                ),
                                seed: seed,
                                observedDirtyEpoch: observedDirtyEpoch
                            ),
                        ],
                        binding: CaptureDeliveryBinding(subscription),
                        onCoalesced: coalesced.record
                    )
                } catch CaptureQueueError.capacityExceeded {
                    capacityDeferred = true
                    break
                } catch NativeAccessError.memberOutsideRoot {
                    // This child left the watched root between the read and the
                    // commit, so its candidate was refused (design §21.8.2). That
                    // is this one membership's condition and not the scope's:
                    // nothing of the refused read is recorded, and the next pass
                    // closes the membership through the ordinary disappearance
                    // path, with the review thresholds that path applies.
                    continue
                }
                // A capture of exactly these bytes already exists for this
                // source, published by another of its routes. This membership
                // binds to it and queues nothing — and is then up to date, not
                // waiting on work of its own.
                guard let entry = (entries + coalesced.entries()).first else { continue }
                member.lastObservedDigest = entry.intent.localIntegrityDigest
                member.lastObservedByteLength = entry.intent.byteLength
                member.lastQueuedItemID = entry.intent.itemId
                Self.replace(member, in: &updated.members)
                if entries.isEmpty {
                    unchanged += 1
                } else {
                    queued += 1
                    unresolvedCount += 1
                }
            } else {
                if unresolvedCount >= CaptureIntentQueue.maximumUnresolvedItems {
                    capacityDeferred = true
                    break
                }
                let memberID = Self.identifier(prefix: "member")
                let accessRef = Self.identifier(prefix: "access")
                // A file this scan has just found may already be a source of
                // this workspace — under another watched root, or under a
                // direct route, or under the name it had before it was
                // renamed. It keeps the identities it has.
                let identities = try canonicalSources.resolve(
                    url: candidate.url,
                    workspaceRef: subscription.workspaceRef
                )
                let documentID = identities.linkedDocumentID
                let sourceIdentity = identities.requestedSourceIdentity
                let descriptor = try accessStore.adoptRootMember(
                    accessRef: accessRef,
                    linkedDocumentID: documentID,
                    rootRef: subscription.rootRef,
                    memberRef: memberID,
                    relativeComponents: candidate.relativeComponents,
                    expectedRootGeneration: expectedGeneration
                )
                let itemID = Self.identifier(prefix: "item")
                var member = FolderSubscriptionMember(
                    memberID: memberID,
                    accessRef: accessRef,
                    linkedDocumentID: documentID,
                    requestedSourceIdentity: sourceIdentity,
                    relativeComponents: candidate.relativeComponents,
                    displayName: candidate.displayName,
                    contentKind: candidate.contentKind,
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
                let seed = Self.seed(
                    subscription: subscription,
                    member: member,
                    itemID: itemID,
                    payloadRef: Self.identifier(prefix: "payload"),
                    operationKey: Self.identifier(prefix: "capture"),
                    base: .expected(nil),
                    bulkRunID: bulkRunID,
                    manifestRevision: manifestRevision,
                    observedAt: observedAt,
                    rootBindingGeneration: expectedGeneration
                )
                let entries: [CaptureQueueEntry]
                let coalesced = CoalescedItems()
                do {
                    entries = try await preparation.prepareAndPublish(
                        [
                            CapturePreparationItem(
                                access: NativeAccessExpectation(
                                    accessRef: accessRef,
                                    generation: descriptor.generation,
                                    rootBindingGeneration: expectedGeneration
                                ),
                                seed: seed,
                                // This member is not persisted until the scan commits.
                                observedDirtyEpoch: 0
                            ),
                        ],
                        binding: CaptureDeliveryBinding(subscription),
                        onCoalesced: coalesced.record
                    )
                } catch CaptureQueueError.capacityExceeded {
                    capacityDeferred = true
                    break
                } catch NativeAccessError.memberOutsideRoot {
                    // The same rule as above for a file this pass had only just
                    // found: it left before its first capture committed, so it is
                    // not enrolled at all rather than enrolled as a member this
                    // root can no longer read. The adoption above is the one
                    // durable trace of it, and nothing owns that grant — no
                    // membership is appended below and no entry was published —
                    // so it goes with the candidate rather than being left as a
                    // route of this root to a file the root cannot read. A
                    // failure to make that removal durable is the scope's
                    // problem and is raised as one: the route is never reported
                    // as gone while it is still recorded.
                    try accessStore.abandonUnownedRootMember(
                        accessRef: accessRef,
                        rootRef: subscription.rootRef,
                        memberRef: memberID,
                        relativeComponents: candidate.relativeComponents,
                        expectedGeneration: descriptor.generation,
                        expectedRootGeneration: expectedGeneration
                    )
                    continue
                }
                // The same rule as above: a source another route already
                // captured at exactly these bytes gains a membership here, not
                // a second capture.
                guard let entry = (entries + coalesced.entries()).first else { continue }
                member.baselineItemID = entry.intent.itemId
                member.lastQueuedItemID = entry.intent.itemId
                member.lastObservedDigest = entry.intent.localIntegrityDigest
                member.lastObservedByteLength = entry.intent.byteLength
                member.lastObservedAt = observedAt
                updated.members.append(member)
                if entries.isEmpty {
                    unchanged += 1
                } else {
                    queued += 1
                    unresolvedCount += 1
                }
            }
        }

        var absent = 0
        for index in updated.members.indices {
            if !candidateKeys.contains(Self.pathKey(updated.members[index].relativeComponents)) {
                updated.members[index].state = .absent
                absent += 1
            }
        }
        updated.lastCompletedMetadataScanAt = observedAt
        // The window survives a completing pass. Resetting it here would mean
        // several quick passes, each below the threshold, never adding up to the
        // burst they are.
        // A pass that got here over the threshold did so on an approval, and what
        // was approved is exempt: it is not counted again, or the quiet pass after
        // a backfill would find a burst of nothing and reopen review.
        if thresholdCrossed {
            updated.changeBurstWindowStartedAt = observedAt
            updated.changeBurstCount = 0
        } else {
            updated.changeBurstWindowStartedAt = burstWindowStartedAt
            updated.changeBurstCount = burstCount
        }
        updated.needsFullReconciliation = capacityDeferred
        if !capacityDeferred {
            updated.pendingReviewSignature = nil
            updated.approvedReviewSignature = nil
            updated.reviewChangeCount = nil
            updated.reviewExamples = nil
        }
        _ = try subscriptions.update(id: subscription.subscriptionID) { $0 = updated }
        return FolderMonitoringReport(
            subscriptionID: subscription.subscriptionID,
            state: updated.state,
            examined: candidates.count,
            queued: queued,
            unchanged: unchanged,
            absent: absent
        )
    }

    private static func seed(
        subscription: FolderSubscription,
        member: FolderSubscriptionMember,
        itemID: String,
        payloadRef: String,
        operationKey: String,
        base: CaptureBasePrecondition,
        bulkRunID: String,
        manifestRevision: String,
        observedAt: String,
        rootBindingGeneration: UInt64
    ) -> CaptureIntentSeed {
        CaptureIntentSeed(
            submissionId: identifier(prefix: "submission"),
            itemId: itemID,
            targetRef: subscription.targetRef,
            workspaceRef: subscription.workspaceRef,
            targetBindingRevision: subscription.targetBindingRevision,
            callerContextBinding: subscription.callerContextBinding,
            surface: .automaticFolder,
            contentKind: member.contentKind,
            payloadRef: payloadRef,
            requestedSourceIdentity: member.requestedSourceIdentity,
            operationKey: operationKey,
            intentKind: .folderCapture,
            basePrecondition: base,
            processingProfile: ProcessingProfileBinding(profileRef: "document-default", revision: "1"),
            originalBinding: OriginalBindingIntent(
                accessRecordId: member.accessRef,
                bindingGeneration: member.membershipGeneration,
                linkedDocumentId: member.linkedDocumentID,
                rootAccessGroupId: subscription.rootRef,
                rootBindingGeneration: rootBindingGeneration,
                memberId: member.memberID,
                membershipGeneration: member.membershipGeneration
            ),
            consent: ConsentBinding(
                subscriptionId: subscription.subscriptionID,
                consentGeneration: subscription.consentGeneration,
                configGeneration: subscription.configGeneration,
                dirtyEpoch: member.dirtyEpoch
            ),
            bulkOrigin: BulkOriginBinding(
                bulkRunId: bulkRunID,
                manifestRevision: manifestRevision,
                memberId: member.memberID
            ),
            createdAt: observedAt,
            displayName: member.displayName
        )
    }

    /// Delegates to the one shared bounded traversal, so a watched root's
    /// actual membership is exactly what the reviewed preview showed.
    private static func scan(
        root: URL,
        recursive: Bool,
        includeHidden: Bool,
        controlledStores: [URL]
    ) throws -> [MonitoredCandidate] {
        let scan: FolderScopeScan
        do {
            scan = try FolderScopePolicy.scan(
                root: root,
                recursive: recursive,
                includeHidden: includeHidden,
                controlledStores: controlledStores
            )
        } catch {
            throw FolderMonitoringError.scopeUnavailable
        }
        // An incomplete scope is never reported as complete coverage. A part of
        // the folder the scan could not read would otherwise look exactly like
        // a part the user deleted, so the subscription needs attention instead.
        guard scan.coverageFailures.isEmpty else {
            throw FolderMonitoringError.scopeCoverageIncomplete
        }
        guard scan.limit == nil else { throw FolderMonitoringError.scopeLimitExceeded }
        return scan.members.map {
            MonitoredCandidate(
                url: $0.url,
                relativeComponents: $0.relativeComponents,
                displayName: $0.displayName,
                contentKind: $0.contentKind,
                byteLength: $0.byteLength
            )
        }
    }

    private static func replace(_ member: FolderSubscriptionMember, in members: inout [FolderSubscriptionMember]) {
        if let index = members.firstIndex(where: { $0.memberID == member.memberID }) {
            members[index] = member
        }
    }

    private static func pathKey(_ components: [String]) -> String {
        components.joined(separator: "\u{0}")
    }

    private static func digest(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    /// What an approval is bound to: the exact reviewed inventory — every changed
    /// member at the size it was reviewed at, and every member reviewed as missing
    /// — under the subscription, configuration and consent the review was shown
    /// for. Any of those moving on makes the approval stale rather than wider.
    static func reviewSignature(
        subscription: FolderSubscription,
        changes: [MonitoredCandidate],
        disappearances: [FolderSubscriptionMember]
    ) -> String {
        let material = ([
            "subscription\u{1f}\(subscription.subscriptionID)",
            "config\u{1f}\(subscription.configGeneration)",
            "consent\u{1f}\(subscription.consentGeneration)",
        ] + changes.map {
            "changed\u{1f}\(pathKey($0.relativeComponents))\u{1f}\($0.byteLength)"
        } + disappearances.map {
            "missing\u{1f}\(pathKey($0.relativeComponents))"
        })
            .sorted()
            .joined(separator: "\u{1e}")
        return digest(Data(material.utf8))
    }

    private static func timestamp(from value: String) -> Date? {
        ISO8601DateFormatter().date(from: value)
    }

    private static func identifier(prefix: String) -> String {
        "\(prefix)-\(UUID().uuidString.lowercased())"
    }
}
