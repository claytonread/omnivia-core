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
    /// The same gate the queue, the subscription stores and the scheduling machine were built with,
    /// so this owner can hold one critical section across all three. See `PublicationGate`.
    private let publicationGate: PublicationGate
    /// Process-monotonic milliseconds, which is what the scheduling machine's periods are measured
    /// in. See `DocumentIntakeEnvironment.processUptimeMilliseconds`.
    private let clock: @Sendable () -> Int64
    /// Test seam: makes the next check's scheduling publication write throw instead of running, so
    /// the split publication `repairableEntry` repairs can be reached deterministically. Production
    /// never sets it.
    private var failNextSchedulingPublication = false

    init(
        accessStore: NativeAccessStore,
        preparation: CapturePreparationCoordinator,
        queue: CaptureIntentQueue,
        subscriptions: FileSubscriptionStore,
        reader: any CoordinatedOriginalReading,
        scheduling: AutomaticSchedulingStateMachine,
        publicationGate: PublicationGate,
        clock: @escaping @Sendable () -> Int64
    ) {
        self.accessStore = accessStore
        self.preparation = preparation
        self.queue = queue
        self.subscriptions = subscriptions
        self.reader = reader
        self.scheduling = scheduling
        self.publicationGate = publicationGate
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

    /// See `failNextSchedulingPublication`.
    func failNextSchedulingPublicationForTesting() {
        failNextSchedulingPublication = true
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
            // The receipts, the identity of whatever this scope published, and the scheduler
            // completion a terminal receipt allows are one critical section on the shared gate, so
            // no relink, pause, reconfigure or publication interleaves between reading that
            // evidence and acting on it.
            guard let resolved = publicationGate.withLock({ applyReceipts(persisted) }) else {
                continue
            }
            reports.append(FileMonitoringReport(
                subscriptionID: resolved.value.subscriptionID,
                state: resolved.value.state,
                queued: false
            ))
            guard resolved.value.state == .active, resolved.evidence == .clear else { continue }
            checkable[Self.schedulingKey(for: resolved.value)] = (resolved.value, reports.count - 1)
        }
        for claim in claimDueWork(among: Set(checkable.keys)) {
            guard let candidate = checkable[claim.key] else { continue }
            reports[candidate.report] = await check(candidate.value, claim: claim)
        }
        return reports
    }

    /// What this scope's publication record says about whether another check may run.
    private enum PublicationEvidence: Equatable {
        /// The published intent has not reached a terminal outcome, so the scope's one candidate
        /// still stands and nothing new is read for it.
        case unresolved
        /// The record does not identify exactly one intent that belongs to this subscription, so
        /// this owner cannot tell a duplicate publication from a legitimate one.
        case unverifiable
        /// The queue reported this scope's published intent's outcome, so the generation that
        /// published it is finished as soon as that receipt is applied.
        case resolved(generation: UInt64)
        case clear
    }

    /// Applies the queue's receipts to one subscription and resolves whatever its scope published:
    /// the review transition unaccountable evidence forces, and the scheduler completion a terminal
    /// receipt allows. Returns nil when the subscription could not be updated at all.
    ///
    /// Called under `publicationGate`, so the receipt effects and the completion that depends on
    /// them are one transition as far as every other publication owner is concerned.
    ///
    /// The published intent is identified from the subscription as it stands *before* any receipt is
    /// applied, because a terminal receipt clears `lastQueuedItemID` — the only ref a legacy
    /// publication has to be found by. Nothing the receipts change takes part in the identity check.
    ///
    /// Replay-safe across the two durable writes: the receipt is recorded first, and if the process
    /// stops before the completion, the next call resolves the same still-published ref against the
    /// same terminal entry, finds the receipt effects already applied, and completes it then.
    private func applyReceipts(
        _ persisted: FileSubscription
    ) -> (value: FileSubscription, evidence: PublicationEvidence)? {
        let evidence = publicationEvidence(for: persisted)
        guard var value = try? reconcileReceipts(persisted) else { return nil }
        switch evidence {
        case let .resolved(generation):
            // Releasing the scheduler's in-flight slot is what exposes any later dirty generation
            // for the next claim, so the completion is what makes this scope checkable again —
            // only a completion that actually happened. A refused one (the durable write failed,
            // say) rolls back whole, leaving the scope published; reporting that as clear would let
            // the next claim publish a second intent for the generation this one still holds. So a
            // refusal keeps the scope unresolved and the completion stays owed: the next call
            // resolves the same still-published ref against the same terminal entry, finds the
            // receipt effects already applied, and completes it then.
            do {
                try scheduling.markCompleted(
                    Self.schedulingKey(for: value),
                    generation: generation,
                    at: clock()
                )
            } catch {
                return (value, .unresolved)
            }
            return (value, .clear)
        case .unverifiable:
            if value.state == .active,
               let reviewed = try? subscriptions.update(
                   id: value.subscriptionID, { $0.state = .reviewRequired }
               )
            {
                value = reviewed
            }
            return (value, .unverifiable)
        case .unresolved, .clear:
            return (value, evidence)
        }
    }

    /// Resolves the scope's published intent, if it has one, and checks it really is this
    /// subscription's automatic capture before anything is allowed to replace it.
    ///
    /// The scheduling machine's ref names that intent exactly, which is what makes the check an
    /// identity check rather than a search for anything that looks close enough. Missing, duplicated
    /// or mismatched evidence is never resolved by guessing: the subscription goes to review, the
    /// scheduler's publication is left exactly as it stands, and no replacement is published.
    ///
    /// A ref that does resolve to exactly this subscription's own automatic capture, and whose
    /// entry the queue has reported a terminal outcome for, names the generation that is now
    /// finished — see `.resolved`, which `applyReceipts` completes once the receipt is applied.
    private func publicationEvidence(for value: FileSubscription) -> PublicationEvidence {
        let entries = queue.allEntries()
        guard let scheduled = scheduling.state(of: Self.schedulingKey(for: value)),
              scheduled.published
        else {
            // No publication is on record, so an unresolved intent naming this subscription is one
            // the record cannot account for — a publication whose scheduling write did not survive,
            // say — and it still holds this scope's one candidate.
            return entries.contains { entry in
                entry.intent.consent?.subscriptionId == value.subscriptionID
                    && [.pending, .staging, .unknownOutcome].contains(entry.state)
            } ? .unresolved : .clear
        }
        // A publication recovered from a document written before refs existed carries none, so the
        // subscription's own last queued item is the only evidence there is. Nothing else falls
        // back to it: a publication that should name its intent and does not is unverifiable.
        guard let ref = scheduled.intentRef
            ?? (scheduled.legacyUnresolvedIntent ? value.lastQueuedItemID : nil)
        else { return .unverifiable }
        let matches = entries.filter { $0.intent.itemId == ref }
        guard matches.count == 1, Self.isAutomaticCapture(matches[0].intent, of: value) else {
            return .unverifiable
        }
        if [.pending, .staging, .unknownOutcome].contains(matches[0].state) { return .unresolved }
        // Published, so the machine's own validation guarantees the generation that published it.
        guard let generation = scheduled.activeGeneration else { return .unverifiable }
        return .resolved(generation: generation)
    }

    /// Whether this intent is the automatic capture this subscription would itself have published:
    /// the same surface and kind, the same delivery binding, the same original, and the same consent
    /// binding down to the dirty epoch it was prepared for.
    private static func isAutomaticCapture(_ intent: CaptureIntentV3, of value: FileSubscription) -> Bool {
        intent.surface == .automaticFile
            && intent.intentKind == .automaticCapture
            && intent.workspaceRef == value.workspaceRef
            && intent.targetRef == value.targetRef
            && intent.targetBindingRevision == value.targetBindingRevision
            && intent.requestedSourceIdentity == value.requestedSourceIdentity
            && intent.callerContextBinding == value.callerContextBinding
            && intent.consent == ConsentBinding(
                subscriptionId: value.subscriptionID,
                consentGeneration: value.consentGeneration,
                configGeneration: value.configGeneration,
                dirtyEpoch: value.dirtyEpoch
            )
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
            let injectedPublicationFailure = failNextSchedulingPublication
            failNextSchedulingPublication = false
            let entries: [CaptureQueueEntry]
            do {
                entries = try await preparation.prepareAndPublish(
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
                    if injectedPublicationFailure {
                        throw AutomaticSchedulingError.stateUnavailable
                    }
                    try scheduling.markPublished(
                        key,
                        generation: generation,
                        intentRef: itemID,
                        at: clock()
                    )
                }
            } catch let failure as CapturePublicationIncomplete {
                // The queue rename is the commit point, so the candidate did reach an intent and
                // only this owner's own record of it is missing. Nothing is read, published or
                // cancelled: the entry that committed is checked to be exactly this subscription's
                // automatic capture for the epoch just prepared, then recorded — the observation
                // first and the scheduler's publication second, as one critical section on the same
                // gate the queue commit was taken on. A throw from any of it leaves the repair owed
                // and falls to the error path below, which resolves the claim from the publication
                // record as it stands rather than guessing at the intent.
                return try publicationGate.withLock {
                    let entry = try Self.repairableEntry(failure, of: value, in: queue)
                    try recordPublication(entry, of: value, checkedAt: checkedAt)
                    try scheduling.markPublished(
                        key,
                        generation: generation,
                        intentRef: entry.intent.itemId,
                        at: clock()
                    )
                    return FileMonitoringReport(
                        subscriptionID: value.subscriptionID,
                        state: value.state,
                        queued: true
                    )
                }
            }
            guard let entry = entries.first else {
                return FileMonitoringReport(
                    subscriptionID: value.subscriptionID,
                    state: value.state,
                    queued: false
                )
            }
            try recordPublication(entry, of: value, checkedAt: checkedAt)
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

    /// Records what this scope's published intent observed. Every field is set to an absolute value
    /// derived from that intent, so applying it twice is applying it once.
    private func recordPublication(
        _ entry: CaptureQueueEntry,
        of value: FileSubscription,
        checkedAt: String
    ) throws {
        _ = try subscriptions.update(id: value.subscriptionID) {
            $0.dirtyEpoch = value.dirtyEpoch
            $0.lastObservedDigest = entry.intent.localIntegrityDigest
            $0.lastObservedByteLength = entry.intent.byteLength
            $0.lastObservedAt = checkedAt
            $0.lastCheckedAt = checkedAt
            $0.lastQueuedItemID = entry.intent.itemId
            $0.needsReconciliation = false
        }
    }

    /// The single entry a split publication committed, or a throw when the evidence does not say
    /// exactly that.
    ///
    /// It has to be one entry, still the one intent that committed, still unresolved, and still this
    /// subscription's own automatic capture down to the dirty epoch it was prepared for — the same
    /// identity check `publicationEvidence` applies to a ref on record. Anything else is left to the
    /// error path: a terminal outcome is the receipt path's to resolve, and missing, duplicated or
    /// mismatched evidence is never repaired by guessing which intent was meant.
    private static func repairableEntry(
        _ failure: CapturePublicationIncomplete,
        of value: FileSubscription,
        in queue: CaptureIntentQueue
    ) throws -> CaptureQueueEntry {
        guard failure.published.count == 1 else { throw failure }
        let entry = failure.published[0]
        let current = queue.allEntries().filter { $0.intent.itemId == entry.intent.itemId }
        guard current.count == 1,
              current[0].intent == entry.intent,
              [.pending, .staging, .unknownOutcome].contains(current[0].state),
              Self.isAutomaticCapture(entry.intent, of: value)
        else { throw failure }
        return entry
    }

    /// Applies the queue's receipts to every subscription without checking anything.
    ///
    /// The same gated flow `reconcile` runs, so a caller that only reconciles receipts still
    /// finishes the scheduler generation a terminal receipt resolves: the terminal outcome is the
    /// whole of what completes a published generation, and which entry point observed it is not
    /// part of that. It claims nothing and reads no source, so nothing is published from here.
    @discardableResult
    public func reconcileReceipts() -> [FileSubscription] {
        subscriptions.all().compactMap { persisted in
            publicationGate.withLock { applyReceipts(persisted)?.value }
        }
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
        // An item ID resolves only when exactly one entry carries it, which is the same rule
        // `publicationEvidence` applies to the published ref: an ID this owner cannot tell apart
        // names nothing, so a receipt is left unreconciled rather than read off an arbitrary one.
        // `CaptureIntentQueue` keys its entries by item ID and refuses a batch whose items repeat
        // one, so a duplicate is unreachable through its API — this is a trap removed, not a case
        // reached today, and it is why the branch has no test of its own.
        let entries = Dictionary(grouping: queue.allEntries(), by: \.intent.itemId)
            .compactMapValues { $0.count == 1 ? $0[0] : nil }
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
