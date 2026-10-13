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
}

private struct MonitoredCandidate: Sendable {
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

    init(
        accessStore: NativeAccessStore,
        preparation: CapturePreparationCoordinator,
        queue: CaptureIntentQueue,
        subscriptions: FolderSubscriptionStore,
        reader: any CoordinatedOriginalReading
    ) {
        self.accessStore = accessStore
        self.preparation = preparation
        self.queue = queue
        self.subscriptions = subscriptions
        self.reader = reader
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
                expectedGeneration: subscription.rootBindingGeneration
            ) { root in
                FolderObservationRoot(subscriptionID: subscription.subscriptionID, url: root)
            }
        }
    }

    private func reconcile(subscriptionIDs: Set<String>?) async -> [FolderMonitoringReport] {
        var reports: [FolderMonitoringReport] = []
        for persisted in subscriptions.all() {
            if let subscriptionIDs, !subscriptionIDs.contains(persisted.subscriptionID) { continue }
            guard let subscription = try? reconcileAdmissions(persisted) else { continue }
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
        _ = try accessStore.releaseRoot(
            rootRef: subscription.rootRef,
            ownerRef: subscription.subscriptionID
        )
        return subscription
    }

    private func reconcileAdmissions(_ persisted: FolderSubscription) throws -> FolderSubscription {
        let entries = Dictionary(uniqueKeysWithValues: queue.allEntries().map { ($0.intent.itemId, $0) })
        return try subscriptions.update(id: persisted.subscriptionID) { value in
            var baselineResolved = true
            var baselineFailed = false
            for index in value.members.indices {
                if let baselineID = value.members[index].baselineItemID,
                   let baseline = entries[baselineID]
                {
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
            expectedGeneration: expectedGeneration
        ) { root in
            try Self.scan(
                root: root,
                recursive: subscription.recursive,
                includeHidden: subscription.includeHidden
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
        let credibleChanges = candidates.filter { candidate in
            guard let existing = existingByPath[Self.pathKey(candidate.relativeComponents)] else {
                return true
            }
            return existing.lastObservedByteLength != candidate.byteLength
        }
        let credibleDisappearances = subscription.members.filter {
            $0.state == .present && !candidateKeys.contains(Self.pathKey($0.relativeComponents))
        }
        let disappearanceThresholdCrossed = credibleDisappearances.count >= 20
            && credibleDisappearances.count * 10 >= max(1, subscription.members.count)
        let reviewSignature = Self.reviewSignature(
            changes: credibleChanges,
            disappearances: credibleDisappearances
        )
        if (credibleChanges.count >= 100 || disappearanceThresholdCrossed),
           subscription.approvedReviewSignature == nil
        {
            let examples = credibleChanges.map(\.displayName)
                + credibleDisappearances.map { $0.displayName + " (missing)" }
            let reviewed = try subscriptions.update(id: subscription.subscriptionID) { value in
                value.state = .reviewRequired
                value.needsFullReconciliation = true
                value.pendingReviewSignature = reviewSignature
                value.reviewChangeCount = credibleChanges.count + credibleDisappearances.count
                value.reviewExamples = Array(examples.prefix(10))
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
        let observedAt = ISO8601DateFormatter().string(from: Date())
        var updated = subscription
        var queued = 0
        var unchanged = 0
        var unresolvedCount = unresolvedEntries.count
        var capacityDeferred = false

        for candidate in candidates {
            let key = Self.pathKey(candidate.relativeComponents)
            if var member = existingByPath[key] {
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
                do {
                    entries = try await preparation.prepareAndPublish([
                        CapturePreparationItem(
                            access: NativeAccessExpectation(
                                accessRef: member.accessRef,
                                generation: member.membershipGeneration,
                                rootBindingGeneration: expectedGeneration
                            ),
                            seed: seed
                        ),
                    ])
                } catch CaptureQueueError.capacityExceeded {
                    capacityDeferred = true
                    break
                }
                guard let entry = entries.first else { continue }
                member.lastObservedDigest = entry.intent.localIntegrityDigest
                member.lastObservedByteLength = entry.intent.byteLength
                member.lastQueuedItemID = entry.intent.itemId
                Self.replace(member, in: &updated.members)
                queued += 1
                unresolvedCount += 1
            } else {
                if unresolvedCount >= CaptureIntentQueue.maximumUnresolvedItems {
                    capacityDeferred = true
                    break
                }
                let memberID = Self.identifier(prefix: "member")
                let accessRef = Self.identifier(prefix: "access")
                let documentID = Self.identifier(prefix: "doc")
                let sourceIdentity = Self.identifier(prefix: "source")
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
                do {
                    entries = try await preparation.prepareAndPublish([
                        CapturePreparationItem(
                            access: NativeAccessExpectation(
                                accessRef: accessRef,
                                generation: descriptor.generation,
                                rootBindingGeneration: expectedGeneration
                            ),
                            seed: seed
                        ),
                    ])
                } catch CaptureQueueError.capacityExceeded {
                    capacityDeferred = true
                    break
                }
                guard let entry = entries.first else { continue }
                member.lastObservedDigest = entry.intent.localIntegrityDigest
                member.lastObservedByteLength = entry.intent.byteLength
                member.lastObservedAt = observedAt
                updated.members.append(member)
                queued += 1
                unresolvedCount += 1
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

    private static func scan(
        root: URL,
        recursive: Bool,
        includeHidden: Bool
    ) throws -> [MonitoredCandidate] {
        let keys: Set<URLResourceKey> = [
            .isRegularFileKey,
            .isDirectoryKey,
            .isSymbolicLinkKey,
            .isHiddenKey,
            .isPackageKey,
            .fileSizeKey,
        ]
        guard let enumerator = FileManager.default.enumerator(
            at: root,
            includingPropertiesForKeys: Array(keys),
            options: [.skipsPackageDescendants],
            errorHandler: { _, _ in true }
        ) else { throw FolderMonitoringError.scopeUnavailable }
        var result: [MonitoredCandidate] = []
        var examined = 0
        for case let candidate as URL in enumerator {
            examined += 1
            guard examined <= 100_000 else { throw FolderMonitoringError.scopeLimitExceeded }
            let values = try candidate.resourceValues(forKeys: keys)
            if values.isSymbolicLink == true {
                if values.isDirectory == true { enumerator.skipDescendants() }
                continue
            }
            let relative = try relativeComponents(root: root, member: candidate)
            guard relative.count <= 32 else {
                if values.isDirectory == true { enumerator.skipDescendants() }
                continue
            }
            if !recursive, relative.count > 1 {
                if values.isDirectory == true { enumerator.skipDescendants() }
                continue
            }
            if !includeHidden, values.isHidden == true {
                if values.isDirectory == true { enumerator.skipDescendants() }
                continue
            }
            if values.isDirectory == true {
                if Self.excludedDirectoryNames.contains(candidate.lastPathComponent) {
                    enumerator.skipDescendants()
                }
                continue
            }
            guard values.isRegularFile == true,
                  let byteLength = values.fileSize,
                  byteLength > 0,
                  !Self.excludedFile(candidate.lastPathComponent),
                  let kind = ManualCaptureSubmission.contentKind(for: candidate)
            else { continue }
            result.append(
                MonitoredCandidate(
                    url: candidate,
                    relativeComponents: relative,
                    displayName: relative.joined(separator: "/"),
                    contentKind: kind,
                    byteLength: UInt64(byteLength)
                )
            )
        }
        return result.sorted { $0.displayName.localizedStandardCompare($1.displayName) == .orderedAscending }
    }

    private static let excludedDirectoryNames: Set<String> = [
        ".git", ".venv", "node_modules", "__pycache__", "DerivedData",
    ]

    private static func excludedFile(_ name: String) -> Bool {
        if name == ".DS_Store" || name.hasPrefix("~$") { return true }
        let lower = name.lowercased()
        return [".swp", ".swo", ".tmp", ".part", ".crdownload"].contains { lower.hasSuffix($0) }
    }

    private static func relativeComponents(root: URL, member: URL) throws -> [String] {
        let rootComponents = root.pathComponents
        let memberComponents = member.standardizedFileURL.pathComponents
        guard memberComponents.count > rootComponents.count,
              Array(memberComponents.prefix(rootComponents.count)) == rootComponents
        else { throw FolderMonitoringError.scopeUnavailable }
        let relative = Array(memberComponents.dropFirst(rootComponents.count))
        guard relative.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }) else {
            throw FolderMonitoringError.scopeUnavailable
        }
        return relative
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

    private static func reviewSignature(
        changes: [MonitoredCandidate],
        disappearances: [FolderSubscriptionMember]
    ) -> String {
        let material = (changes.map {
            "changed\u{1f}\(pathKey($0.relativeComponents))\u{1f}\($0.byteLength)"
        } + disappearances.map {
            "missing\u{1f}\(pathKey($0.relativeComponents))"
        })
            .sorted()
            .joined(separator: "\u{1e}")
        return digest(Data(material.utf8))
    }

    private static func identifier(prefix: String) -> String {
        "\(prefix)-\(UUID().uuidString.lowercased())"
    }
}
