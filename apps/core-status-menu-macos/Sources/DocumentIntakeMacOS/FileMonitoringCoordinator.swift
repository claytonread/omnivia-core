import CryptoKit
import Foundation

public struct FileMonitoringReport: Equatable, Sendable {
    public let subscriptionID: String
    public let state: FolderSubscriptionState
    public let queued: Bool
}

public actor FileMonitoringCoordinator {
    /// An individual watch has exactly one scope: the original it was registered for.
    static let individualFileScopeKey = "individual-file"

    private let accessStore: NativeAccessStore
    private let preparation: CapturePreparationCoordinator
    private let queue: CaptureIntentQueue
    private let subscriptions: FileSubscriptionStore
    private let reader: any CoordinatedOriginalReading
    private let scheduling: AutomaticSchedulingStateMachine
    /// Process-monotonic milliseconds, which is what the scheduling machine's periods are measured
    /// in. See `DocumentIntakeEnvironment.processUptimeMilliseconds`.
    private let clock: @Sendable () -> Int64

    init(
        accessStore: NativeAccessStore,
        preparation: CapturePreparationCoordinator,
        queue: CaptureIntentQueue,
        subscriptions: FileSubscriptionStore,
        reader: any CoordinatedOriginalReading,
        scheduling: AutomaticSchedulingStateMachine,
        clock: @escaping @Sendable () -> Int64
    ) {
        self.accessStore = accessStore
        self.preparation = preparation
        self.queue = queue
        self.subscriptions = subscriptions
        self.reader = reader
        self.scheduling = scheduling
        self.clock = clock
    }

    /// The scheduling unit an individual watch signals under: the workspace and the verified
    /// original's own opaque source identity, both fixed when the subscription was registered and
    /// neither a path nor a bookmark. Every event for one original therefore coalesces into that
    /// scope's single dirty record however many events arrive, and a relink that bumps the binding
    /// generation or re-resolves the bookmark keeps the same key.
    public static func schedulingKey(for subscription: FileSubscription) -> AutomaticSchedulingKey {
        AutomaticSchedulingKey(
            source: AutomaticSchedulingSource(
                workspaceRef: subscription.workspaceRef,
                sourceRef: subscription.requestedSourceIdentity
            ),
            scopeKey: individualFileScopeKey
        )
    }

    @discardableResult
    public func reconcileAll() async -> [FileMonitoringReport] {
        await reconcile(subscriptionIDs: nil)
    }

    @discardableResult
    public func reconcile(subscriptionIDs: Set<String>) async -> [FileMonitoringReport] {
        await reconcile(subscriptionIDs: Optional(subscriptionIDs))
    }

    public func markDirty(subscriptionIDs: Set<String>) {
        for id in subscriptionIDs {
            guard let value = try? subscriptions.update(id: id, { value in
                guard value.state == .active else { return }
                value.needsReconciliation = true
            }), value.state == .active else { continue }
            // One generation per call, whatever shape the event had. The scope's dirty record
            // keeps its first-signal time, so the maximum wait is measured from the first of a
            // burst, and tracks this latest one, so the quiet period is measured from the last.
            _ = try? scheduling.signal(
                Self.schedulingKey(for: value),
                priority: .background,
                at: clock()
            )
        }
    }

    public func activeObservationRoots() -> [FolderObservationRoot] {
        subscriptions.all().compactMap { value in
            guard value.state == .active else { return nil }
            return try? accessStore.withReadAccess(
                accessRef: value.accessRef,
                expectedGeneration: value.bindingGeneration
            ) { url in
                // FSEvents may report either the file or its containing directory. Watching the
                // parent makes both forms an advisory reconciliation hint for this subscription.
                FolderObservationRoot(
                    subscriptionID: value.subscriptionID,
                    url: url.deletingLastPathComponent()
                )
            }
        }
    }

    /// Revalidates every in-scope subscription, then reads and publishes for exactly the scopes the
    /// scheduler says are due.
    ///
    /// Nothing outside a claim touches a source: a file whose quiet period has not elapsed, or
    /// whose candidate is already in flight, is reported as it stands and left alone. Consent,
    /// config and binding revalidation, receipt reconciliation and the manual entry points are
    /// unchanged — only the decision of *when* to read moved to the scheduler.
    private func reconcile(subscriptionIDs: Set<String>?) async -> [FileMonitoringReport] {
        var reports: [FileMonitoringReport] = []
        var checkable: [AutomaticSchedulingKey: (value: FileSubscription, report: Int)] = [:]
        for persisted in subscriptions.all() {
            if let subscriptionIDs, !subscriptionIDs.contains(persisted.subscriptionID) { continue }
            guard let value = try? reconcileReceipts(persisted) else { continue }
            reports.append(FileMonitoringReport(
                subscriptionID: value.subscriptionID,
                state: value.state,
                queued: false
            ))
            guard value.state == .active else { continue }
            let unresolved = queue.allEntries().contains { entry in
                entry.intent.consent?.subscriptionId == value.subscriptionID
                    && [.pending, .staging, .unknownOutcome].contains(entry.state)
            }
            if unresolved { continue }
            checkable[Self.schedulingKey(for: value)] = (value, reports.count - 1)
        }
        for claim in claimDueWork(among: Set(checkable.keys)) {
            guard let candidate = checkable[claim.key] else { continue }
            reports[candidate.report] = await check(candidate.value, claim: claim)
        }
        return reports
    }

    /// Claims the due scopes this call will check, and only those. A scope this call is not
    /// checking — paused, filtered out of this call, or not an individual file at all — is never
    /// selected, so its due work is neither claimed nor handed back: it keeps its one dirty
    /// obligation, its turn in the rotation and its place among this call's claims for whoever
    /// owns it. A claim stays in flight for the rest of the call, so no scope is selected twice.
    private func claimDueWork(among allowed: Set<AutomaticSchedulingKey>) -> [AutomaticSchedulingClaim] {
        var claims: [AutomaticSchedulingClaim] = []
        while claims.count < allowed.count,
              let claim = try? scheduling.claim(at: clock(), among: allowed) {
            claims.append(claim)
        }
        return claims
    }

    /// Carries out one claimed check: read the original, then either finish the generation because
    /// nothing changed or publish the changed bytes as this scope's one unresolved intent.
    private func check(
        _ subscription: FileSubscription,
        claim: AutomaticSchedulingClaim
    ) async -> FileMonitoringReport {
        var value = subscription
        do {
            let bytes = try accessStore.withReadAccess(
                accessRef: value.accessRef,
                expectedGeneration: value.bindingGeneration
            ) { url in
                try reader.readStableBytes(from: url, maximumBytes: CaptureIntentV3.maximumPayloadBytes)
            }
            let digest = Self.digest(bytes)
            let checkedAt = ISO8601DateFormatter().string(from: Date())
            guard digest != value.lastObservedDigest else {
                // The read happened and is what established there is nothing new, so this
                // generation is finished rather than returned to dirty for another attempt.
                try scheduling.markCompletedWithoutPublication(
                    claim.key,
                    generation: claim.generation,
                    at: clock()
                )
                _ = try subscriptions.update(id: value.subscriptionID) {
                    $0.lastCheckedAt = checkedAt
                    $0.needsReconciliation = false
                }
                return FileMonitoringReport(
                    subscriptionID: value.subscriptionID,
                    state: value.state,
                    queued: false
                )
            }
            let observedDirtyEpoch = value.dirtyEpoch
            value.dirtyEpoch += 1
            let itemID = Self.identifier(prefix: "item")
            let seed = CaptureIntentSeed(
                submissionId: Self.identifier(prefix: "submission"),
                itemId: itemID,
                targetRef: value.targetRef,
                workspaceRef: value.workspaceRef,
                targetBindingRevision: value.targetBindingRevision,
                callerContextBinding: value.callerContextBinding,
                surface: .automaticFile,
                contentKind: value.contentKind,
                payloadRef: Self.identifier(prefix: "payload"),
                requestedSourceIdentity: value.requestedSourceIdentity,
                operationKey: Self.identifier(prefix: "capture"),
                intentKind: .automaticCapture,
                basePrecondition: .expected(value.currentSuccessfulCaptureID),
                processingProfile: ProcessingProfileBinding(
                    profileRef: "document-default",
                    revision: "1"
                ),
                originalBinding: OriginalBindingIntent(
                    accessRecordId: value.accessRef,
                    bindingGeneration: value.bindingGeneration,
                    linkedDocumentId: value.linkedDocumentID
                ),
                consent: ConsentBinding(
                    subscriptionId: value.subscriptionID,
                    consentGeneration: value.consentGeneration,
                    configGeneration: value.configGeneration,
                    dirtyEpoch: value.dirtyEpoch
                ),
                createdAt: checkedAt,
                displayName: value.displayName
            )
            let scheduling = self.scheduling
            let clock = self.clock
            let key = claim.key
            let generation = claim.generation
            let entries = try await preparation.prepareAndPublish(
                [
                    CapturePreparationItem(
                        access: NativeAccessExpectation(
                            accessRef: value.accessRef,
                            generation: value.bindingGeneration
                        ),
                        seed: seed,
                        observedDirtyEpoch: observedDirtyEpoch
                    ),
                ],
                binding: CaptureDeliveryBinding(
                    targetRef: value.targetRef,
                    workspaceRef: value.workspaceRef,
                    targetBindingRevision: value.targetBindingRevision,
                    callerContextBinding: value.callerContextBinding
                )
            ) { published in
                // Inside the publication critical section, under the gate the queue commit was
                // taken on: the scope's candidate reached an intent, named by the item ID the
                // queue published it under. This batch is always exactly one item.
                guard let itemID = published.first?.intent.itemId else { return }
                try scheduling.markPublished(
                    key,
                    generation: generation,
                    intentRef: itemID,
                    at: clock()
                )
            }
            guard let entry = entries.first else {
                return FileMonitoringReport(
                    subscriptionID: value.subscriptionID,
                    state: value.state,
                    queued: false
                )
            }
            _ = try subscriptions.update(id: value.subscriptionID) {
                $0.dirtyEpoch = value.dirtyEpoch
                $0.lastObservedDigest = entry.intent.localIntegrityDigest
                $0.lastObservedByteLength = entry.intent.byteLength
                $0.lastObservedAt = checkedAt
                $0.lastCheckedAt = checkedAt
                $0.lastQueuedItemID = entry.intent.itemId
                $0.needsReconciliation = false
            }
            return FileMonitoringReport(
                subscriptionID: value.subscriptionID,
                state: value.state,
                queued: true
            )
        } catch {
            // The read or the preparation failed, so nothing was published and the generation is
            // owed again: it returns to the scope's one dirty obligation, keeping its signal times
            // so it stays as due as it already was. This call does not claim it again.
            if scheduling.state(of: claim.key)?.published != true {
                _ = try? scheduling.cancel(claim.key, generation: claim.generation, at: clock())
            }
            _ = try? subscriptions.update(id: value.subscriptionID) {
                $0.state = .needsAccess
                $0.needsReconciliation = true
            }
            return FileMonitoringReport(
                subscriptionID: value.subscriptionID,
                state: .needsAccess,
                queued: false
            )
        }
    }

    @discardableResult
    public func reconcileReceipts() -> [FileSubscription] {
        subscriptions.all().compactMap { try? reconcileReceipts($0) }
    }

    @discardableResult
    public func pause(subscriptionID: String) throws -> FileSubscription {
        try subscriptions.update(id: subscriptionID) {
            guard $0.state == .active || $0.state == .needsAccess else {
                throw FolderMonitoringError.subscriptionNotActive
            }
            $0.state = .paused
            $0.needsReconciliation = true
        }
    }

    @discardableResult
    public func resume(subscriptionID: String) throws -> FileSubscription {
        try subscriptions.update(id: subscriptionID) {
            guard $0.state == .paused || $0.state == .needsAccess else {
                throw FolderMonitoringError.subscriptionNotActive
            }
            $0.state = .active
            $0.needsReconciliation = true
        }
    }

    @discardableResult
    public func stop(subscriptionID: String) throws -> FileSubscription {
        try subscriptions.update(id: subscriptionID) {
            $0.state = .stopped
            $0.needsReconciliation = false
        }
    }

    private func reconcileReceipts(_ persisted: FileSubscription) throws -> FileSubscription {
        let entries = Dictionary(uniqueKeysWithValues: queue.allEntries().map { ($0.intent.itemId, $0) })
        return try subscriptions.update(id: persisted.subscriptionID) { value in
            if let baseline = entries[value.baselineItemID] {
                if baseline.state == .admitted, let captureID = baseline.admittedCaptureID {
                    if value.state == .waitingForCore || value.currentSuccessfulCaptureID == nil {
                        value.currentSuccessfulCaptureID = captureID
                    }
                    if value.state == .waitingForCore || value.lastObservedDigest == nil {
                        value.lastObservedDigest = baseline.intent.localIntegrityDigest
                        value.lastObservedByteLength = baseline.intent.byteLength
                        value.lastObservedAt = baseline.intent.createdAt
                    }
                    if value.state == .waitingForCore { value.state = .active }
                } else if baseline.state == .failed, value.state == .waitingForCore {
                    value.state = .reviewRequired
                }
            }
            if let queuedID = value.lastQueuedItemID,
               let queued = entries[queuedID]
            {
                if queued.state == .admitted, let captureID = queued.admittedCaptureID {
                    value.currentSuccessfulCaptureID = captureID
                    value.lastQueuedItemID = nil
                } else if queued.state == .failed {
                    // The source digest was recorded when this version was durably queued. Clear
                    // it after a terminal refusal so Resume can deliberately prepare a new intent
                    // instead of silently treating the rejected version as synchronized.
                    value.lastQueuedItemID = nil
                    value.lastObservedDigest = nil
                    value.lastObservedByteLength = nil
                    value.lastObservedAt = nil
                    value.needsReconciliation = true
                    value.state = .paused
                }
            }
        }
    }

    private static func digest(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private static func identifier(prefix: String) -> String {
        "\(prefix)-\(UUID().uuidString.lowercased())"
    }
}
