import CryptoKit
import Foundation

public struct FileMonitoringReport: Equatable, Sendable {
    public let subscriptionID: String
    public let state: FolderSubscriptionState
    public let queued: Bool
}

public actor FileMonitoringCoordinator {
    private let accessStore: NativeAccessStore
    private let preparation: CapturePreparationCoordinator
    private let queue: CaptureIntentQueue
    private let subscriptions: FileSubscriptionStore
    private let reader: any CoordinatedOriginalReading

    init(
        accessStore: NativeAccessStore,
        preparation: CapturePreparationCoordinator,
        queue: CaptureIntentQueue,
        subscriptions: FileSubscriptionStore,
        reader: any CoordinatedOriginalReading
    ) {
        self.accessStore = accessStore
        self.preparation = preparation
        self.queue = queue
        self.subscriptions = subscriptions
        self.reader = reader
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
            _ = try? subscriptions.update(id: id) {
                guard $0.state == .active else { return }
                $0.needsReconciliation = true
            }
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

    private func reconcile(subscriptionIDs: Set<String>?) async -> [FileMonitoringReport] {
        var reports: [FileMonitoringReport] = []
        for persisted in subscriptions.all() {
            if let subscriptionIDs, !subscriptionIDs.contains(persisted.subscriptionID) { continue }
            guard var value = try? reconcileReceipts(persisted) else { continue }
            guard value.state == .active else {
                reports.append(FileMonitoringReport(
                    subscriptionID: value.subscriptionID,
                    state: value.state,
                    queued: false
                ))
                continue
            }
            let unresolved = queue.allEntries().contains { entry in
                entry.intent.consent?.subscriptionId == value.subscriptionID
                    && [.pending, .staging, .unknownOutcome].contains(entry.state)
            }
            if unresolved {
                reports.append(FileMonitoringReport(
                    subscriptionID: value.subscriptionID,
                    state: value.state,
                    queued: false
                ))
                continue
            }
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
                    _ = try subscriptions.update(id: value.subscriptionID) {
                        $0.lastCheckedAt = checkedAt
                        $0.needsReconciliation = false
                    }
                    reports.append(FileMonitoringReport(
                        subscriptionID: value.subscriptionID,
                        state: value.state,
                        queued: false
                    ))
                    continue
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
                )
                guard let entry = entries.first else { continue }
                _ = try subscriptions.update(id: value.subscriptionID) {
                    $0.dirtyEpoch = value.dirtyEpoch
                    $0.lastObservedDigest = entry.intent.localIntegrityDigest
                    $0.lastObservedByteLength = entry.intent.byteLength
                    $0.lastObservedAt = checkedAt
                    $0.lastCheckedAt = checkedAt
                    $0.lastQueuedItemID = entry.intent.itemId
                    $0.needsReconciliation = false
                }
                reports.append(FileMonitoringReport(
                    subscriptionID: value.subscriptionID,
                    state: value.state,
                    queued: true
                ))
            } catch {
                _ = try? subscriptions.update(id: value.subscriptionID) {
                    $0.state = .needsAccess
                    $0.needsReconciliation = true
                }
                reports.append(FileMonitoringReport(
                    subscriptionID: value.subscriptionID,
                    state: .needsAccess,
                    queued: false
                ))
            }
        }
        return reports
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
