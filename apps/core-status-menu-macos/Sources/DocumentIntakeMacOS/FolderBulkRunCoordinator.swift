import Foundation

public struct FolderBulkRunReport: Equatable, Sendable {
    public let runID: String
    public let state: FolderBulkRunState
    public let planned: Int
    public let published: Int
    public let admitted: Int
    public let needsAttention: Int
    public let stopped: Int
    public let total: Int
    public let newlyPublished: [CaptureQueueEntry]

    /// Exact per-file progress. A saved manifest is never reported as every
    /// file added.
    public var progressDetail: String {
        "\(admitted) added · \(published) in progress · \(planned) waiting · "
            + "\(stopped) stopped · \(needsAttention) needs attention · of \(total)"
    }
}

/// Collects what `CapturePreparationCoordinator` coalesced, from the thread its
/// publication critical section runs on.
final class CoalescedItems: @unchecked Sendable {
    private let lock = NSLock()
    private var byRequestedItem: [String: CaptureQueueEntry] = [:]

    var record: CaptureCoalescenceCompletion {
        { [self] pairs in
            lock.withLock {
                for pair in pairs { byRequestedItem[pair.requestedItemID] = pair.existing }
            }
        }
    }

    /// Requested item id to the id of the capture that serves it.
    func all() -> [String: String] {
        lock.withLock { byRequestedItem.mapValues { $0.intent.itemId } }
    }

    /// The serving captures themselves, each named once.
    func entries() -> [CaptureQueueEntry] {
        lock.withLock {
            Dictionary(
                byRequestedItem.values.map { ($0.intent.itemId, $0) },
                uniquingKeysWith: { first, _ in first }
            ).values.sorted { $0.intent.itemId < $1.intent.itemId }
        }
    }
}

/// Progresses frozen folder plans in ordinary bounded queue batches. The durable run ledger is the
/// source of truth for unstarted members, so queue capacity never forces truncation or rediscovery.
public actor FolderBulkRunCoordinator {
    public static let maximumBatchItems = 10
    public static let maximumBatchBytes: UInt64 = 200 * 1024 * 1024

    private let preparation: CapturePreparationCoordinator
    private let queue: CaptureIntentQueue
    private let runs: FolderBulkRunStore
    private let accessStore: NativeAccessStore

    init(
        accessStore: NativeAccessStore,
        preparation: CapturePreparationCoordinator,
        queue: CaptureIntentQueue,
        runs: FolderBulkRunStore
    ) {
        self.accessStore = accessStore
        self.preparation = preparation
        self.queue = queue
        self.runs = runs
    }

    @discardableResult
    public func advanceAll() async -> [FolderBulkRunReport] {
        var reports: [FolderBulkRunReport] = []
        for run in runs.all() {
            if Self.isTerminal(run.state) {
                try? finishRoot(for: run)
            } else if [.adding, .needsAttention].contains(run.state) {
                reports.append(await advance(runID: run.runID))
            }
        }
        return reports
    }

    @discardableResult
    public func advance(runID: String) async -> FolderBulkRunReport {
        guard var run = runs.run(id: runID) else {
            return FolderBulkRunReport(
                runID: runID,
                state: .needsAttention,
                planned: 0,
                published: 0,
                admitted: 0,
                needsAttention: 1,
                stopped: 0,
                total: 0,
                newlyPublished: []
            )
        }
        run = (try? reconcile(run)) ?? run
        if Self.isTerminal(run.state) {
            try? finishRoot(for: run)
        }
        guard run.state == .adding else { return report(run, newlyPublished: []) }

        let unresolved = queue.allEntries().filter {
            [.pending, .staging, .unknownOutcome].contains($0.state)
        }
        var availableItems = max(0, CaptureIntentQueue.maximumUnresolvedItems - unresolved.count)
        var availableBytes = CaptureIntentQueue.maximumUnresolvedPayloadBytes
            - min(
                CaptureIntentQueue.maximumUnresolvedPayloadBytes,
                unresolved.reduce(UInt64(0)) { $0 + $1.intent.byteLength }
            )
        guard availableItems > 0, availableBytes > 0 else {
            return report(run, newlyPublished: [])
        }

        var selected: [FolderBulkRunMember] = []
        var selectedBytes: UInt64 = 0
        for member in run.members where member.state == .planned {
            guard availableItems > 0 else { break }
            guard member.declaredByteLength <= availableBytes,
                  selectedBytes + member.declaredByteLength <= Self.maximumBatchBytes
            else { continue }
            selected.append(member)
            selectedBytes += member.declaredByteLength
            availableItems -= 1
            availableBytes -= member.declaredByteLength
            if selected.count == Self.maximumBatchItems { break }
        }
        guard !selected.isEmpty else { return report(run, newlyPublished: []) }

        let items = selected.map { preparationItem(run: run, member: $0) }
        let binding = Self.binding(of: run)
        var published: [CaptureQueueEntry] = []
        // Members whose canonical source already held an initial capture of
        // exactly their bytes: this run queued nothing for them, and binds them
        // to the capture that serves them instead.
        let coalesced = CoalescedItems()
        do {
            published = try await preparation.prepareAndPublish(
                items,
                binding: binding,
                onCoalesced: coalesced.record
            )
        } catch CaptureQueueError.capacityExceeded {
            return report(run, newlyPublished: [])
        } catch {
            // A bad member must not cancel every other frozen member. Isolate the bounded batch,
            // preserving each item's identity for an explicit retry of only the failed source.
            for item in items {
                do {
                    published.append(
                        contentsOf: try await preparation.prepareAndPublish(
                            [item],
                            binding: binding,
                            onCoalesced: coalesced.record
                        )
                    )
                } catch CaptureQueueError.capacityExceeded {
                    break
                } catch {
                    _ = try? runs.update(id: run.runID) { value in
                        guard let index = value.members.firstIndex(where: {
                            $0.itemID == item.seed.itemId
                        }) else { return }
                        value.members[index].state = .needsAttention
                        value.members[index].failureCode = "preparation_failed"
                    }
                }
            }
        }
        let served = coalesced.all()
        if !published.isEmpty || !served.isEmpty {
            let publishedIDs = Set(published.map { $0.intent.itemId })
            run = (try? runs.update(id: run.runID) { value in
                for index in value.members.indices {
                    let itemID = value.members[index].itemID
                    if let existing = served[itemID] {
                        value.members[index].coalescedItemID = existing
                    } else if !publishedIDs.contains(itemID) {
                        continue
                    }
                    value.members[index].state = .published
                    value.members[index].failureCode = nil
                }
                value.state = Self.derivedState(value.members)
            }) ?? run
        } else {
            run = runs.run(id: run.runID) ?? run
        }
        if Self.isTerminal(run.state) {
            try? finishRoot(for: run)
        }
        return report(run, newlyPublished: published)
    }

    @discardableResult
    public func reconcileReceipts() -> [FolderBulkRun] {
        runs.all().compactMap { run in
            guard let reconciled = try? reconcile(run) else { return nil }
            if Self.isTerminal(reconciled.state) {
                try? finishRoot(for: reconciled)
            }
            return reconciled
        }
    }

    @discardableResult
    public func pause(runID: String) throws -> FolderBulkRun {
        try runs.update(id: runID) { value in
            guard value.state == .adding else { return }
            value.state = .paused
        }
    }

    /// Resumes member reads. An item that already failed keeps its own outcome;
    /// retrying those is the separate, explicit **Retry unavailable files**.
    @discardableResult
    public func resume(runID: String) throws -> FolderBulkRun {
        try runs.update(id: runID) { value in
            guard value.state == .paused else { return }
            value.state = Self.derivedState(value.members)
        }
    }

    /// Acts on the same remaining frozen membership after legitimate access
    /// recovery. It discovers nothing: a member that was never in the manifest
    /// cannot appear here.
    @discardableResult
    public func retryUnavailable(runID: String) throws -> FolderBulkRun {
        try runs.update(id: runID) { value in
            for index in value.members.indices where value.members[index].state == .needsAttention {
                value.members[index].state = .planned
                value.members[index].failureCode = nil
            }
            if value.state != .paused { value.state = Self.derivedState(value.members) }
        }
    }

    /// Cancels every remaining unpublished member — both the unstarted ones and
    /// the ones awaiting attention, which are equally unpublished. An already
    /// published or admitted child keeps its canonical outcome, and nothing
    /// already in Core is removed.
    @discardableResult
    public func stopRemaining(runID: String) throws -> FolderBulkRun {
        let run = try runs.update(id: runID) { value in
            for index in value.members.indices
            where [.planned, .needsAttention].contains(value.members[index].state) {
                value.members[index].state = .stopped
            }
            value.state = Self.derivedState(value.members)
        }
        if Self.isTerminal(run.state) { try finishRoot(for: run) }
        return run
    }

    private func reconcile(_ run: FolderBulkRun) throws -> FolderBulkRun {
        let entries = Dictionary(uniqueKeysWithValues: queue.allEntries().map { ($0.intent.itemId, $0) })
        return try runs.update(id: run.runID) { value in
            for index in value.members.indices {
                let served = value.members[index].coalescedItemID ?? value.members[index].itemID
                guard let entry = entries[served] else { continue }
                switch entry.state {
                case .admitted:
                    value.members[index].state = .admitted
                    value.members[index].failureCode = nil
                case .failed:
                    value.members[index].state = .needsAttention
                    value.members[index].failureCode = entry.failureCode ?? "capture_failed"
                case .pending, .staging, .unknownOutcome:
                    value.members[index].state = .published
                }
            }
            if value.state != .paused { value.state = Self.derivedState(value.members) }
        }
    }

    private func preparationItem(
        run: FolderBulkRun,
        member: FolderBulkRunMember
    ) -> CapturePreparationItem {
        let automatic = run.subscriptionID != nil
        let seed = CaptureIntentSeed(
            submissionId: run.submissionID,
            itemId: member.itemID,
            targetRef: run.targetRef,
            workspaceRef: run.workspaceRef,
            targetBindingRevision: run.targetBindingRevision,
            callerContextBinding: run.callerContextBinding,
            surface: automatic ? .automaticFolder : .folderImport,
            contentKind: member.contentKind,
            payloadRef: member.payloadRef,
            requestedSourceIdentity: member.requestedSourceIdentity,
            operationKey: member.operationKey,
            intentKind: .folderCapture,
            basePrecondition: .expected(nil),
            processingProfile: ProcessingProfileBinding(
                profileRef: "document-default",
                revision: "1"
            ),
            originalBinding: OriginalBindingIntent(
                accessRecordId: member.accessRef,
                bindingGeneration: member.bindingGeneration,
                linkedDocumentId: member.linkedDocumentID,
                rootAccessGroupId: run.rootRef,
                rootBindingGeneration: run.rootBindingGeneration,
                memberId: member.memberID,
                membershipGeneration: member.bindingGeneration
            ),
            consent: run.subscriptionID.map {
                ConsentBinding(
                    subscriptionId: $0,
                    consentGeneration: 1,
                    configGeneration: 1,
                    dirtyEpoch: 1
                )
            },
            bulkOrigin: BulkOriginBinding(
                bulkRunId: run.runID,
                manifestRevision: run.manifestRevision,
                memberId: member.memberID
            ),
            createdAt: run.createdAt,
            displayName: member.displayName
        )
        return CapturePreparationItem(
            access: NativeAccessExpectation(
                accessRef: member.accessRef,
                generation: member.bindingGeneration,
                rootBindingGeneration: seed.originalBinding?.rootBindingGeneration
            ),
            seed: seed,
            // A run's members are planned at the subscription's first epoch, and
            // monitoring cannot advance it until every reviewed baseline — that
            // is, every member of this run — has been admitted.
            observedDirtyEpoch: automatic ? 1 : nil
        )
    }

    private static func binding(of run: FolderBulkRun) -> CaptureDeliveryBinding {
        CaptureDeliveryBinding(
            targetRef: run.targetRef,
            workspaceRef: run.workspaceRef,
            targetBindingRevision: run.targetBindingRevision,
            callerContextBinding: run.callerContextBinding
        )
    }

    private func report(
        _ run: FolderBulkRun,
        newlyPublished: [CaptureQueueEntry]
    ) -> FolderBulkRunReport {
        FolderBulkRunReport(
            runID: run.runID,
            state: run.state,
            planned: run.plannedCount,
            published: run.publishedCount,
            admitted: run.admittedCount,
            needsAttention: run.attentionCount,
            stopped: run.stoppedCount,
            total: run.members.count,
            newlyPublished: newlyPublished
        )
    }

    private static func derivedState(_ members: [FolderBulkRunMember]) -> FolderBulkRunState {
        if members.contains(where: { $0.state == .planned || $0.state == .published }) { return .adding }
        if members.contains(where: { $0.state == .needsAttention }) { return .needsAttention }
        if members.allSatisfy({ $0.state == .stopped }) { return .stopped }
        return .complete
    }

    private static func isTerminal(_ state: FolderBulkRunState) -> Bool {
        state == .complete || state == .stopped
    }

    /// The plan is over. Its root group narrows to `frozen_members_only`, so it
    /// keeps serving manual Refresh/Open/Reveal for the children that were
    /// actually enrolled and can neither discover nor add another member. The
    /// grant itself is only released when the last owned route is gone, which
    /// `releaseRoot` decides — a watcher or another approved import that still
    /// owns it keeps it alive.
    private func finishRoot(for run: FolderBulkRun) throws {
        // A watch on the same root still owns discovery; only its own stop
        // narrows the group.
        if run.subscriptionID == nil {
            _ = try? accessStore.narrowRootAuthority(
                rootRef: run.rootRef,
                to: .frozenMembersOnly
            )
        }
        _ = try accessStore.releaseRoot(rootRef: run.rootRef, ownerRef: run.runID)
    }
}
