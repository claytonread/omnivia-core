import Foundation

public struct FolderBulkRunReport: Equatable, Sendable {
    public let runID: String
    public let state: FolderBulkRunState
    public let planned: Int
    public let published: Int
    public let admitted: Int
    public let needsAttention: Int
    public let newlyPublished: [CaptureQueueEntry]
}

/// Progresses frozen folder plans in ordinary bounded queue batches. The durable run ledger is the
/// source of truth for unstarted members, so queue capacity never forces truncation or rediscovery.
public actor FolderBulkRunCoordinator {
    public static let maximumBatchItems = 10
    public static let maximumBatchBytes: UInt64 = 200 * 1024 * 1024

    private let preparation: CapturePreparationCoordinator
    private let queue: CaptureIntentQueue
    private let runs: FolderBulkRunStore

    init(
        preparation: CapturePreparationCoordinator,
        queue: CaptureIntentQueue,
        runs: FolderBulkRunStore
    ) {
        self.preparation = preparation
        self.queue = queue
        self.runs = runs
    }

    @discardableResult
    public func advanceAll() async -> [FolderBulkRunReport] {
        var reports: [FolderBulkRunReport] = []
        for run in runs.all() where [.adding, .needsAttention].contains(run.state) {
            reports.append(await advance(runID: run.runID))
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
                newlyPublished: []
            )
        }
        run = (try? reconcile(run)) ?? run
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
        var published: [CaptureQueueEntry] = []
        do {
            published = try await preparation.prepareAndPublish(items)
        } catch CaptureQueueError.capacityExceeded {
            return report(run, newlyPublished: [])
        } catch {
            // A bad member must not cancel every other frozen member. Isolate the bounded batch,
            // preserving each item's identity for an explicit retry of only the failed source.
            for item in items {
                do {
                    published.append(contentsOf: try await preparation.prepareAndPublish([item]))
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
        if !published.isEmpty {
            let publishedIDs = Set(published.map { $0.intent.itemId })
            run = (try? runs.update(id: run.runID) { value in
                for index in value.members.indices where publishedIDs.contains(value.members[index].itemID) {
                    value.members[index].state = .published
                    value.members[index].failureCode = nil
                }
                value.state = Self.derivedState(value.members)
            }) ?? run
        } else {
            run = runs.run(id: run.runID) ?? run
        }
        return report(run, newlyPublished: published)
    }

    @discardableResult
    public func reconcileReceipts() -> [FolderBulkRun] {
        runs.all().compactMap { try? reconcile($0) }
    }

    @discardableResult
    public func pause(runID: String) throws -> FolderBulkRun {
        try runs.update(id: runID) { value in
            guard value.state == .adding else { return }
            value.state = .paused
        }
    }

    @discardableResult
    public func resume(runID: String) throws -> FolderBulkRun {
        try runs.update(id: runID) { value in
            guard value.state == .paused || value.state == .needsAttention else { return }
            for index in value.members.indices where value.members[index].state == .needsAttention {
                value.members[index].state = .planned
                value.members[index].failureCode = nil
            }
            value.state = .adding
        }
    }

    @discardableResult
    public func stopRemaining(runID: String) throws -> FolderBulkRun {
        try runs.update(id: runID) { value in
            for index in value.members.indices where value.members[index].state == .planned {
                value.members[index].state = .stopped
            }
            value.state = Self.derivedState(value.members)
        }
    }

    private func reconcile(_ run: FolderBulkRun) throws -> FolderBulkRun {
        let entries = Dictionary(uniqueKeysWithValues: queue.allEntries().map { ($0.intent.itemId, $0) })
        return try runs.update(id: run.runID) { value in
            for index in value.members.indices {
                guard let entry = entries[value.members[index].itemID] else { continue }
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
                rootBindingGeneration: 1,
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
            seed: seed
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
            newlyPublished: newlyPublished
        )
    }

    private static func derivedState(_ members: [FolderBulkRunMember]) -> FolderBulkRunState {
        if members.contains(where: { $0.state == .planned || $0.state == .published }) { return .adding }
        if members.contains(where: { $0.state == .needsAttention }) { return .needsAttention }
        if members.allSatisfy({ $0.state == .stopped }) { return .stopped }
        return .complete
    }
}
