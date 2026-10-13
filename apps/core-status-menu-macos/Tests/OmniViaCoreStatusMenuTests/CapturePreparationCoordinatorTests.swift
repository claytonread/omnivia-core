import Foundation
import XCTest
@testable import DocumentIntakeMacOS

private final class PreparationBookmarkCodec: SecurityScopedBookmarkCoding, @unchecked Sendable {
    func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
    func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
        (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
    }
    func startAccessing(_ url: URL) -> Bool { true }
    func stopAccessing(_ url: URL) {}
}

private final class TestOriginalReader: CoordinatedOriginalReading, @unchecked Sendable {
    private let onRead: @Sendable (URL) throws -> Data

    init(_ onRead: @escaping @Sendable (URL) throws -> Data) {
        self.onRead = onRead
    }

    func readStableBytes(from url: URL, maximumBytes: UInt64) throws -> Data {
        let data = try onRead(url)
        guard UInt64(data.count) <= maximumBytes else { throw CapturePreparationError.fileTooLarge }
        return data
    }
}

/// Lets a reader reach the coordinator that owns it.
private final class CoordinatorBox: @unchecked Sendable {
    var value: CapturePreparationCoordinator?
}

/// Carries what a competing thread saw back to the test body.
private final class OutcomeBox: @unchecked Sendable {
    var entry: CaptureQueueEntry?
    var failure: Error?
}

private let stageDigest = "sha256:" + String(repeating: "a", count: 64)
private let descriptorDigest = "sha256:" + String(repeating: "b", count: 64)

private let callerBinding = CallerContextBinding(
    clientContextRef: "client-1",
    principalRef: "principal-1",
    contextRevision: "context-1"
)

private let publicationBinding = CaptureDeliveryBinding(
    targetRef: "target-1",
    workspaceRef: "workspace-1",
    targetBindingRevision: "target-binding-1",
    callerContextBinding: callerBinding
)

final class CapturePreparationCoordinatorTests: XCTestCase {
    private func temporaryRoot() throws -> URL {
        let root = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("omnivia-preparation-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return root
    }

    private func makeAccessStore(_ root: URL) throws -> NativeAccessStore {
        try NativeAccessStore(
            stateDirectory: root.appendingPathComponent("access"),
            codec: PreparationBookmarkCodec()
        )
    }

    private func makeQueue(_ root: URL) throws -> CaptureIntentQueue {
        try CaptureIntentQueue(root: root.appendingPathComponent("queue"))
    }

    /// Payload bytes a rejected publication must never leave behind.
    private func payloadFiles(_ root: URL) -> [String] {
        let payloads = root.appendingPathComponent("queue", isDirectory: true)
            .appendingPathComponent("payloads", isDirectory: true)
        return (try? FileManager.default.contentsOfDirectory(atPath: payloads.path)) ?? []
    }

    @discardableResult
    private func adoptFile(
        _ accessStore: NativeAccessStore,
        root: URL,
        item: String,
        access: String
    ) throws -> URL {
        let file = root.appendingPathComponent(item + ".pdf")
        try Data("body-\(item)".utf8).write(to: file)
        _ = try accessStore.adopt(
            accessRef: access,
            linkedDocumentID: "document-" + item,
            selectedURL: file,
            route: .directFile
        )
        return file
    }

    private func seed(
        item: String,
        access: String?,
        generation: UInt64 = 1,
        submission: String = "submission-1"
    ) -> CaptureIntentSeed {
        CaptureIntentSeed(
            submissionId: submission,
            itemId: item,
            targetRef: "target-1",
            workspaceRef: "workspace-1",
            targetBindingRevision: "target-binding-1",
            callerContextBinding: callerBinding,
            surface: .manual,
            contentKind: .pdf,
            payloadRef: "payload-" + item,
            requestedSourceIdentity: "source-" + item,
            operationKey: "operation-" + item,
            intentKind: .refresh,
            basePrecondition: .expected(nil),
            processingProfile: ProcessingProfileBinding(
                profileRef: "profile-1",
                revision: "profile-revision-1"
            ),
            originalBinding: access.map {
                OriginalBindingIntent(
                    accessRecordId: $0,
                    bindingGeneration: generation,
                    linkedDocumentId: "document-" + item
                )
            },
            createdAt: "2026-10-14T00:00:00Z",
            displayName: item + ".pdf"
        )
    }

    private func automaticSeed(
        item: String,
        access: String,
        subscription: String,
        consentGeneration: UInt64 = 1,
        configGeneration: UInt64 = 1
    ) -> CaptureIntentSeed {
        CaptureIntentSeed(
            submissionId: "submission-1",
            itemId: item,
            targetRef: "target-1",
            workspaceRef: "workspace-1",
            targetBindingRevision: "target-binding-1",
            callerContextBinding: callerBinding,
            surface: .automaticFile,
            contentKind: .pdf,
            payloadRef: "payload-" + item,
            requestedSourceIdentity: "source-" + item,
            operationKey: "operation-" + item,
            intentKind: .automaticCapture,
            basePrecondition: .expected(nil),
            processingProfile: ProcessingProfileBinding(
                profileRef: "profile-1",
                revision: "profile-revision-1"
            ),
            originalBinding: OriginalBindingIntent(
                accessRecordId: access,
                bindingGeneration: 1,
                linkedDocumentId: "document-" + item
            ),
            consent: ConsentBinding(
                subscriptionId: subscription,
                consentGeneration: consentGeneration,
                configGeneration: configGeneration,
                // Monitoring freezes the next epoch and persists it only after
                // the item is published.
                dirtyEpoch: 2
            ),
            createdAt: "2026-10-14T00:00:00Z",
            displayName: item + ".pdf"
        )
    }

    private func fileSubscription(
        item: String,
        access: String,
        subscription: String
    ) -> FileSubscription {
        FileSubscription(
            subscriptionID: subscription,
            accessRef: access,
            linkedDocumentID: "document-" + item,
            requestedSourceIdentity: "source-" + item,
            displayName: item + ".pdf",
            contentKind: .pdf,
            bindingGeneration: 1,
            baselineItemID: "baseline-" + item,
            targetRef: "target-1",
            workspaceRef: "workspace-1",
            targetBindingRevision: "target-binding-1",
            callerContextBinding: callerBinding,
            configGeneration: 1,
            consentGeneration: 1,
            consentRecordedAt: "2026-10-14T00:00:00Z",
            state: .active,
            currentSuccessfulCaptureID: nil,
            lastObservedDigest: nil,
            lastObservedByteLength: nil,
            lastObservedAt: nil,
            lastQueuedItemID: nil,
            dirtyEpoch: 1,
            needsReconciliation: false,
            lastCheckedAt: nil
        )
    }

    func testPublishesCompletePreparedSelectionWithFrozenBytes() async throws {
        let root = try temporaryRoot()
        let accessStore = try makeAccessStore(root)
        let queue = try makeQueue(root)
        var items: [CapturePreparationItem] = []
        for index in 1...2 {
            let item = "item-\(index)"
            let access = "access-\(index)"
            try adoptFile(accessStore, root: root, item: item, access: access)
            items.append(
                CapturePreparationItem(
                    access: NativeAccessExpectation(accessRef: access, generation: 1),
                    seed: seed(item: item, access: access)
                )
            )
        }
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: TestOriginalReader { try Data(contentsOf: $0) }
        )

        let published = try await coordinator.prepareAndPublish(items, binding: publicationBinding)

        XCTAssertEqual(published.map { $0.intent.itemId }, ["item-1", "item-2"])
        XCTAssertEqual(try Data(contentsOf: queue.payloadURL(for: "item-1")), Data("body-item-1".utf8))
        XCTAssertEqual(queue.allEntries().count, 2)
    }

    func testGenerationChangeAfterReadsRejectsWholeBatch() async throws {
        let root = try temporaryRoot()
        let accessStore = try makeAccessStore(root)
        let queue = try makeQueue(root)
        var items: [CapturePreparationItem] = []
        for index in 1...2 {
            let item = "item-\(index)"
            let access = "access-\(index)"
            try adoptFile(accessStore, root: root, item: item, access: access)
            items.append(
                CapturePreparationItem(
                    access: NativeAccessExpectation(accessRef: access, generation: 1),
                    seed: seed(item: item, access: access)
                )
            )
        }
        // Only the second grant is relinked, after both originals were read.
        let reader = TestOriginalReader { url in
            let data = try Data(contentsOf: url)
            if url.lastPathComponent == "item-2.pdf" {
                _ = try accessStore.disconnect(accessRef: "access-2")
                _ = try accessStore.adopt(
                    accessRef: "access-2",
                    linkedDocumentID: "document-item-2",
                    selectedURL: url,
                    route: .directFile
                )
            }
            return data
        }
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: reader
        )

        do {
            _ = try await coordinator.prepareAndPublish(items, binding: publicationBinding)
            XCTFail("Expected generation mismatch")
        } catch {
            XCTAssertEqual(error as? NativeAccessError, .generationMismatch)
        }
        XCTAssertTrue(queue.allEntries().isEmpty)
        XCTAssertTrue(payloadFiles(root).isEmpty)
    }

    func testBindingMismatchFailsBeforeReadingOrPublishing() async throws {
        let root = try temporaryRoot()
        let accessStore = try makeAccessStore(root)
        let queue = try makeQueue(root)
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: TestOriginalReader { _ in XCTFail("must not read"); return Data() }
        )
        let item = CapturePreparationItem(
            access: NativeAccessExpectation(accessRef: "other-access", generation: 1),
            seed: seed(item: "item-1", access: "access-1")
        )

        do {
            _ = try await coordinator.prepareAndPublish([item], binding: publicationBinding)
            XCTFail("Expected invalid binding")
        } catch {
            XCTAssertEqual(error as? CapturePreparationError, .invalidBinding)
        }
        XCTAssertTrue(queue.allEntries().isEmpty)
    }

    func testCancellationDuringPreparationRejectsWholeBatch() async throws {
        let root = try temporaryRoot()
        let accessStore = try makeAccessStore(root)
        let queue = try makeQueue(root)
        var items: [CapturePreparationItem] = []
        for index in 1...2 {
            let item = "item-\(index)"
            let access = "access-\(index)"
            try adoptFile(accessStore, root: root, item: item, access: access)
            items.append(
                CapturePreparationItem(
                    access: NativeAccessExpectation(accessRef: access, generation: 1),
                    seed: seed(item: item, access: access)
                )
            )
        }
        // The coordinator is cancelled while the first original is still being read.
        let box = CoordinatorBox()
        let reader = TestOriginalReader { url in
            box.value?.cancel(submissionId: "submission-1")
            return try Data(contentsOf: url)
        }
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: reader
        )
        box.value = coordinator

        do {
            _ = try await coordinator.prepareAndPublish(items, binding: publicationBinding)
            XCTFail("Expected cancellation")
        } catch {
            XCTAssertEqual(error as? CapturePreparationError, .cancelled)
        }
        XCTAssertTrue(queue.allEntries().isEmpty)
        XCTAssertTrue(payloadFiles(root).isEmpty)
    }

    func testSnapshotOnlySelectionStillValidatesCancellation() async throws {
        let root = try temporaryRoot()
        let accessStore = try makeAccessStore(root)
        let queue = try makeQueue(root)
        try adoptFile(accessStore, root: root, item: "item-1", access: "access-1")
        let box = CoordinatorBox()
        let reader = TestOriginalReader { url in
            box.value?.cancel(submissionId: "submission-1")
            return try Data(contentsOf: url)
        }
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: reader
        )
        box.value = coordinator
        // A snapshot item freezes no original binding at all.
        let item = CapturePreparationItem(
            access: NativeAccessExpectation(accessRef: "access-1", generation: 1),
            seed: seed(item: "item-1", access: nil)
        )

        do {
            _ = try await coordinator.prepareAndPublish([item], binding: publicationBinding)
            XCTFail("Expected cancellation")
        } catch {
            XCTAssertEqual(error as? CapturePreparationError, .cancelled)
        }
        XCTAssertTrue(queue.allEntries().isEmpty)
        XCTAssertTrue(payloadFiles(root).isEmpty)
    }

    func testCancelledSubmissionIsRefusedBeforeAnyRead() async throws {
        let root = try temporaryRoot()
        let accessStore = try makeAccessStore(root)
        let queue = try makeQueue(root)
        try adoptFile(accessStore, root: root, item: "item-1", access: "access-1")
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: TestOriginalReader { _ in XCTFail("must not read"); return Data() }
        )
        coordinator.cancel(submissionId: "submission-1")
        let item = CapturePreparationItem(
            access: NativeAccessExpectation(accessRef: "access-1", generation: 1),
            seed: seed(item: "item-1", access: "access-1")
        )

        do {
            _ = try await coordinator.prepareAndPublish([item], binding: publicationBinding)
            XCTFail("Expected cancellation")
        } catch {
            XCTAssertEqual(error as? CapturePreparationError, .cancelled)
        }
        XCTAssertTrue(queue.allEntries().isEmpty)
        XCTAssertTrue(payloadFiles(root).isEmpty)
    }

    func testFrozenCallerBindingMustMatchThePublicationBinding() async throws {
        let foreignBindings = [
            CaptureDeliveryBinding(
                targetRef: "target-2",
                workspaceRef: "workspace-1",
                targetBindingRevision: "target-binding-1",
                callerContextBinding: callerBinding
            ),
            CaptureDeliveryBinding(
                targetRef: "target-1",
                workspaceRef: "workspace-2",
                targetBindingRevision: "target-binding-1",
                callerContextBinding: callerBinding
            ),
            CaptureDeliveryBinding(
                targetRef: "target-1",
                workspaceRef: "workspace-1",
                targetBindingRevision: "target-binding-2",
                callerContextBinding: callerBinding
            ),
            CaptureDeliveryBinding(
                targetRef: "target-1",
                workspaceRef: "workspace-1",
                targetBindingRevision: "target-binding-1",
                callerContextBinding: CallerContextBinding(
                    clientContextRef: "client-1",
                    principalRef: "principal-2",
                    contextRevision: "context-1"
                )
            ),
        ]
        for binding in foreignBindings {
            let root = try temporaryRoot()
            let accessStore = try makeAccessStore(root)
            let queue = try makeQueue(root)
            var items: [CapturePreparationItem] = []
            for index in 1...2 {
                let item = "item-\(index)"
                let access = "access-\(index)"
                try adoptFile(accessStore, root: root, item: item, access: access)
                items.append(
                    CapturePreparationItem(
                        access: NativeAccessExpectation(accessRef: access, generation: 1),
                        seed: seed(item: item, access: access)
                    )
                )
            }
            let coordinator = CapturePreparationCoordinator(
                accessStore: accessStore,
                queue: queue,
                reader: TestOriginalReader { try Data(contentsOf: $0) }
            )

            do {
                _ = try await coordinator.prepareAndPublish(items, binding: binding)
                XCTFail("Expected binding mismatch for \(binding)")
            } catch {
                XCTAssertEqual(error as? CapturePreparationError, .bindingMismatch)
            }
            XCTAssertTrue(queue.allEntries().isEmpty)
            XCTAssertTrue(payloadFiles(root).isEmpty)
        }
    }

    func testAutomaticSubscriptionChangeDuringPreparationRejectsWholeBatch() async throws {
        let disturbances: [(name: String, change: (inout FileSubscription) -> Void)] = [
            ("dirty epoch", { $0.dirtyEpoch = 7 }),
            ("suppressed state", { $0.state = .paused }),
            ("stopped state", { $0.state = .stopped }),
        ]
        for disturbance in disturbances {
            let root = try temporaryRoot()
            let accessStore = try makeAccessStore(root)
            let queue = try makeQueue(root)
            let subscriptions = try FileSubscriptionStore(
                stateDirectory: root.appendingPathComponent("file-subscriptions")
            )
            var items: [CapturePreparationItem] = []
            for index in 1...2 {
                let item = "item-\(index)"
                let access = "access-\(index)"
                let subscription = "subscription-\(index)"
                try adoptFile(accessStore, root: root, item: item, access: access)
                try subscriptions.register(
                    fileSubscription(item: item, access: access, subscription: subscription)
                )
                items.append(
                    CapturePreparationItem(
                        access: NativeAccessExpectation(accessRef: access, generation: 1),
                        seed: automaticSeed(item: item, access: access, subscription: subscription),
                        observedDirtyEpoch: 1
                    )
                )
            }
            // Only the second watch moves, while the originals are being read.
            let reader = TestOriginalReader { url in
                let data = try Data(contentsOf: url)
                _ = try subscriptions.update(id: "subscription-2", disturbance.change)
                return data
            }
            let coordinator = CapturePreparationCoordinator(
                accessStore: accessStore,
                queue: queue,
                reader: reader,
                fileSubscriptions: subscriptions
            )

            do {
                _ = try await coordinator.prepareAndPublish(items, binding: publicationBinding)
                XCTFail("Expected consent rejection for \(disturbance.name)")
            } catch {
                XCTAssertEqual(
                    error as? CapturePreparationError,
                    .consentChanged,
                    disturbance.name
                )
            }
            XCTAssertTrue(queue.allEntries().isEmpty, disturbance.name)
            XCTAssertTrue(payloadFiles(root).isEmpty, disturbance.name)
        }
    }

    func testAutomaticConsentAndConfigGenerationsMustMatchThePersistedWatch() async throws {
        let mismatches: [(name: String, consent: UInt64, config: UInt64)] = [
            ("consent generation", 2, 1),
            ("config generation", 1, 2),
        ]
        for mismatch in mismatches {
            let root = try temporaryRoot()
            let accessStore = try makeAccessStore(root)
            let queue = try makeQueue(root)
            let subscriptions = try FileSubscriptionStore(
                stateDirectory: root.appendingPathComponent("file-subscriptions")
            )
            try adoptFile(accessStore, root: root, item: "item-1", access: "access-1")
            try subscriptions.register(
                fileSubscription(item: "item-1", access: "access-1", subscription: "subscription-1")
            )
            let coordinator = CapturePreparationCoordinator(
                accessStore: accessStore,
                queue: queue,
                reader: TestOriginalReader { try Data(contentsOf: $0) },
                fileSubscriptions: subscriptions
            )
            let item = CapturePreparationItem(
                access: NativeAccessExpectation(accessRef: "access-1", generation: 1),
                seed: automaticSeed(
                    item: "item-1",
                    access: "access-1",
                    subscription: "subscription-1",
                    consentGeneration: mismatch.consent,
                    configGeneration: mismatch.config
                ),
                observedDirtyEpoch: 1
            )

            do {
                _ = try await coordinator.prepareAndPublish([item], binding: publicationBinding)
                XCTFail("Expected consent rejection for \(mismatch.name)")
            } catch {
                XCTAssertEqual(error as? CapturePreparationError, .consentChanged, mismatch.name)
            }
            XCTAssertTrue(queue.allEntries().isEmpty, mismatch.name)
            XCTAssertTrue(payloadFiles(root).isEmpty, mismatch.name)
        }
    }

    func testAutomaticItemPublishesWhileItsWatchIsUnchanged() async throws {
        let root = try temporaryRoot()
        let accessStore = try makeAccessStore(root)
        let queue = try makeQueue(root)
        let subscriptions = try FileSubscriptionStore(
            stateDirectory: root.appendingPathComponent("file-subscriptions")
        )
        try adoptFile(accessStore, root: root, item: "item-1", access: "access-1")
        try subscriptions.register(
            fileSubscription(item: "item-1", access: "access-1", subscription: "subscription-1")
        )
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: TestOriginalReader { try Data(contentsOf: $0) },
            fileSubscriptions: subscriptions
        )
        let item = CapturePreparationItem(
            access: NativeAccessExpectation(accessRef: "access-1", generation: 1),
            seed: automaticSeed(item: "item-1", access: "access-1", subscription: "subscription-1"),
            observedDirtyEpoch: 1
        )

        let published = try await coordinator.prepareAndPublish([item], binding: publicationBinding)

        XCTAssertEqual(published.map { $0.intent.itemId }, ["item-1"])
        XCTAssertEqual(queue.allEntries().count, 1)
    }

    func testAutomaticItemWithoutAnObservedEpochIsRefused() async throws {
        let root = try temporaryRoot()
        let accessStore = try makeAccessStore(root)
        let queue = try makeQueue(root)
        try adoptFile(accessStore, root: root, item: "item-1", access: "access-1")
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: TestOriginalReader { _ in XCTFail("must not read"); return Data() }
        )
        let item = CapturePreparationItem(
            access: NativeAccessExpectation(accessRef: "access-1", generation: 1),
            seed: automaticSeed(item: "item-1", access: "access-1", subscription: "subscription-1")
        )

        do {
            _ = try await coordinator.prepareAndPublish([item], binding: publicationBinding)
            XCTFail("Expected invalid binding")
        } catch {
            XCTAssertEqual(error as? CapturePreparationError, .invalidBinding)
        }
        XCTAssertTrue(queue.allEntries().isEmpty)
    }

    func testSubscriptionPauseCannotCompleteBetweenConsentValidationAndCommit() async throws {
        let root = try temporaryRoot()
        let gate = PublicationGate()
        let accessStore = try NativeAccessStore(
            stateDirectory: root.appendingPathComponent("access"),
            codec: PreparationBookmarkCodec(),
            gate: gate
        )
        let queue = try CaptureIntentQueue(root: root.appendingPathComponent("queue"), gate: gate)
        let subscriptions = try FileSubscriptionStore(
            stateDirectory: root.appendingPathComponent("file-subscriptions"),
            gate: gate
        )
        try adoptFile(accessStore, root: root, item: "item-1", access: "access-1")
        try subscriptions.register(
            fileSubscription(item: "item-1", access: "access-1", subscription: "subscription-1")
        )
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: TestOriginalReader { try Data(contentsOf: $0) },
            fileSubscriptions: subscriptions,
            gate: gate
        )
        let started = DispatchSemaphore(value: 0)
        let paused = DispatchSemaphore(value: 0)
        // A pause raised after consent was revalidated and before the commit.
        coordinator.afterConsentValidation = {
            Thread.detachNewThread {
                started.signal()
                _ = try? subscriptions.update(id: "subscription-1") { $0.state = .paused }
                paused.signal()
            }
            started.wait()
            XCTAssertEqual(
                paused.wait(timeout: .now() + 0.5),
                .timedOut,
                "the pause must block on the publication gate"
            )
            // Re-entering the store from the thread that holds the gate still
            // sees the consent this publication was validated against.
            XCTAssertEqual(subscriptions.all().first?.state, .active)
        }
        let item = CapturePreparationItem(
            access: NativeAccessExpectation(accessRef: "access-1", generation: 1),
            seed: automaticSeed(item: "item-1", access: "access-1", subscription: "subscription-1"),
            observedDirtyEpoch: 1
        )

        let published = try await coordinator.prepareAndPublish([item], binding: publicationBinding)

        XCTAssertEqual(published.map { $0.intent.itemId }, ["item-1"])
        XCTAssertEqual(paused.wait(timeout: .now() + 5), .success)
        XCTAssertEqual(subscriptions.all().first?.state, .paused)
        XCTAssertEqual(queue.allEntries().count, 1)
    }

    func testDeliveryStageCannotObserveTheBatchBeforeItCommits() async throws {
        let root = try temporaryRoot()
        let gate = PublicationGate()
        let accessStore = try NativeAccessStore(
            stateDirectory: root.appendingPathComponent("access"),
            codec: PreparationBookmarkCodec(),
            gate: gate
        )
        let queue = try CaptureIntentQueue(root: root.appendingPathComponent("queue"), gate: gate)
        try adoptFile(accessStore, root: root, item: "item-1", access: "access-1")
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: TestOriginalReader { try Data(contentsOf: $0) },
            gate: gate
        )
        let started = DispatchSemaphore(value: 0)
        let claimed = DispatchSemaphore(value: 0)
        let outcome = OutcomeBox()
        // A delivery claim raised while the batch is validated but not committed.
        coordinator.afterConsentValidation = {
            Thread.detachNewThread {
                started.signal()
                do {
                    outcome.entry = try queue.beginStage(
                        itemID: "item-1",
                        requestDigest: stageDigest,
                        descriptorDigest: descriptorDigest
                    )
                } catch {
                    outcome.failure = error
                }
                claimed.signal()
            }
            started.wait()
            XCTAssertEqual(
                claimed.wait(timeout: .now() + 0.5),
                .timedOut,
                "the claim must block on the publication gate"
            )
            XCTAssertTrue(queue.allEntries().isEmpty, "nothing is committed yet")
        }
        let item = CapturePreparationItem(
            access: NativeAccessExpectation(accessRef: "access-1", generation: 1),
            seed: seed(item: "item-1", access: "access-1")
        )

        let published = try await coordinator.prepareAndPublish([item], binding: publicationBinding)

        XCTAssertEqual(published.map { $0.intent.itemId }, ["item-1"])
        XCTAssertEqual(claimed.wait(timeout: .now() + 5), .success)
        // The claim ran only after the commit, so it never saw an absent or
        // half-published item: it found the committed one and staged it.
        XCTAssertNil(outcome.failure)
        XCTAssertEqual(outcome.entry?.state, .staging)
        XCTAssertEqual(outcome.entry?.attemptGeneration, 1)
    }

    /// Guards the production wiring itself: every owner the publication
    /// boundary reaches has to have been built with the environment's one gate.
    func testProductionEnvironmentSharesOneGateAcrossEveryPublicationOwner() throws {
        let root = try temporaryRoot()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root,
            bookmarkCodec: PreparationBookmarkCodec()
        )
        let owners: [(name: String, read: @Sendable () -> Void)] = [
            ("access store", { _ = environment.accessStore.descriptors() }),
            ("capture queue", { _ = environment.queue.allEntries() }),
            ("file subscriptions", { _ = environment.fileSubscriptions.all() }),
            ("folder subscriptions", { _ = environment.folderSubscriptions.all() }),
        ]

        for owner in owners {
            let started = DispatchSemaphore(value: 0)
            let finished = DispatchSemaphore(value: 0)
            environment.publicationGate.lockForTesting()
            Thread.detachNewThread {
                started.signal()
                owner.read()
                finished.signal()
            }
            started.wait()
            XCTAssertEqual(
                finished.wait(timeout: .now() + 0.3),
                .timedOut,
                "\(owner.name) does not take the shared gate"
            )
            environment.publicationGate.unlockForTesting()
            XCTAssertEqual(finished.wait(timeout: .now() + 5), .success, owner.name)
        }
    }
}
