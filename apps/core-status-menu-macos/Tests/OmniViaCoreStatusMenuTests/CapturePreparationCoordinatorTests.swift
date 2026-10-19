import CryptoKit
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

    /// Payload bytes a rejected publication must never leave behind, wherever
    /// they could be: the legacy payload directory, an unpublished candidate or
    /// a published batch.
    private func payloadFiles(_ root: URL) -> [String] {
        let queue = root.appendingPathComponent("queue", isDirectory: true)
        guard let children = FileManager.default.enumerator(at: queue, includingPropertiesForKeys: nil)
        else { return [] }
        return (children.allObjects as? [URL] ?? [])
            .filter { $0.pathExtension == "payload" }
            .map(\.lastPathComponent)
            .sorted()
    }

    private func queueNames(_ root: URL, in directory: String) -> [String] {
        ((try? FileManager.default.contentsOfDirectory(
            atPath: root.appendingPathComponent("queue", isDirectory: true)
                .appendingPathComponent(directory, isDirectory: true).path
        )) ?? []).sorted()
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

    /// The payload bytes of a two-item selection are complete on disk before the
    /// final authority lock, and publication inside it is only the rename.
    func testEveryPayloadByteIsWrittenBeforeTheFinalCriticalSection() async throws {
        let root = try temporaryRoot()
        let gate = PublicationGate()
        let accessStore = try NativeAccessStore(
            stateDirectory: root.appendingPathComponent("access"),
            codec: PreparationBookmarkCodec(),
            gate: gate
        )
        let queue = try CaptureIntentQueue(root: root.appendingPathComponent("queue"), gate: gate)
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
            reader: TestOriginalReader { try Data(contentsOf: $0) },
            gate: gate
        )
        coordinator.afterConsentValidation = {
            // Inside the critical section: the bytes are already durable in one
            // candidate directory and nothing is published yet.
            XCTAssertEqual(self.queueNames(root, in: "candidates").count, 1)
            XCTAssertEqual(self.payloadFiles(root), ["payload-item-1.payload", "payload-item-2.payload"])
            XCTAssertTrue(self.queueNames(root, in: "published").isEmpty)
        }

        let published = try await coordinator.prepareAndPublish(items, binding: publicationBinding)

        XCTAssertEqual(published.map { $0.intent.itemId }, ["item-1", "item-2"])
        XCTAssertEqual(queueNames(root, in: "published").count, 1)
        XCTAssertTrue(queueNames(root, in: "candidates").isEmpty)
        XCTAssertEqual(payloadFiles(root), ["payload-item-1.payload", "payload-item-2.payload"])
    }

    // MARK: - Under-gate publication completion

    /// One shared gate and the four owners an automatic publication touches.
    private func automaticFixture(_ root: URL) throws -> (
        gate: PublicationGate,
        accessStore: NativeAccessStore,
        queue: CaptureIntentQueue,
        subscriptions: FileSubscriptionStore,
        scheduling: AutomaticSchedulingStateMachine
    ) {
        let gate = PublicationGate()
        let accessStore = try NativeAccessStore(
            stateDirectory: root.appendingPathComponent("access"),
            codec: PreparationBookmarkCodec(),
            gate: gate
        )
        return (
            gate: gate,
            accessStore: accessStore,
            queue: try CaptureIntentQueue(root: root.appendingPathComponent("queue"), gate: gate),
            subscriptions: try FileSubscriptionStore(
                stateDirectory: root.appendingPathComponent("file-subscriptions"),
                gate: gate
            ),
            scheduling: try AutomaticSchedulingStateMachine(
                stateDirectory: root.appendingPathComponent("automatic-scheduling"),
                gate: gate
            )
        )
    }

    private var schedulingKey: AutomaticSchedulingKey {
        AutomaticSchedulingKey(
            source: AutomaticSchedulingSource(workspaceRef: "workspace-1", sourceRef: "source-item-1"),
            scopeKey: "scope-1"
        )
    }

    /// Signals and claims the one scope, so there is an in-flight generation a
    /// publication may be recorded against.
    @discardableResult
    private func claimScope(
        _ scheduling: AutomaticSchedulingStateMachine
    ) throws -> AutomaticSchedulingClaim {
        try scheduling.signal(schedulingKey, priority: .user, at: 0)
        let claim = try scheduling.claim(at: AutomaticSchedulingStateMachine.quietPeriodMilliseconds)
        return try XCTUnwrap(claim)
    }

    private func automaticItem(
        subscription: String = "subscription-1",
        epoch: UInt64 = 1
    ) -> CapturePreparationItem {
        CapturePreparationItem(
            access: NativeAccessExpectation(accessRef: "access-1", generation: 1),
            seed: automaticSeed(item: "item-1", access: "access-1", subscription: subscription),
            observedDirtyEpoch: epoch
        )
    }

    /// The scheduler obligation is recorded in the same critical section as the
    /// queue commit: the completion sees a published queue and still holds the
    /// gate that every other owner has to wait for.
    func testCompletionRunsAfterPublicationWithTheGateStillHeld() async throws {
        let root = try temporaryRoot()
        let fixture = try automaticFixture(root)
        try adoptFile(fixture.accessStore, root: root, item: "item-1", access: "access-1")
        try fixture.subscriptions.register(
            fileSubscription(item: "item-1", access: "access-1", subscription: "subscription-1")
        )
        let claim = try claimScope(fixture.scheduling)
        let coordinator = CapturePreparationCoordinator(
            accessStore: fixture.accessStore,
            queue: fixture.queue,
            reader: TestOriginalReader { try Data(contentsOf: $0) },
            fileSubscriptions: fixture.subscriptions,
            gate: fixture.gate
        )
        let started = DispatchSemaphore(value: 0)
        let read = DispatchSemaphore(value: 0)
        let observed = OutcomeBox()

        let published = try await coordinator.prepareAndPublish(
            [automaticItem()],
            binding: publicationBinding
        ) { entries in
            // Already committed: the candidate was renamed into the published
            // namespace and the entry is in the queue.
            observed.entry = entries.first
            XCTAssertEqual(entries.map { $0.intent.itemId }, ["item-1"])
            XCTAssertEqual(fixture.queue.allEntries().map { $0.intent.itemId }, ["item-1"])
            XCTAssertEqual(self.queueNames(root, in: "published").count, 1)
            XCTAssertTrue(self.queueNames(root, in: "candidates").isEmpty)
            // And still under the gate, so no other owner can read between the
            // commit and this update.
            Thread.detachNewThread {
                started.signal()
                _ = fixture.queue.allEntries()
                read.signal()
            }
            started.wait()
            XCTAssertEqual(
                read.wait(timeout: .now() + 0.5),
                .timedOut,
                "the completion must still hold the publication gate"
            )
            try fixture.scheduling.markPublished(
                self.schedulingKey,
                generation: claim.generation,
                intentRef: entries[0].intent.itemId,
                at: AutomaticSchedulingStateMachine.quietPeriodMilliseconds
            )
        }

        XCTAssertEqual(published.map { $0.intent.itemId }, ["item-1"])
        XCTAssertEqual(observed.entry?.state, .pending)
        XCTAssertEqual(read.wait(timeout: .now() + 5), .success)
        let state = try XCTUnwrap(fixture.scheduling.state(of: schedulingKey))
        XCTAssertTrue(state.published)
        XCTAssertEqual(state.intentRef, "item-1")
        XCTAssertEqual(state.activeGeneration, claim.generation)
    }

    /// The whole window is one critical section: a pause raised after consent was
    /// revalidated lands neither before the rename nor before the completion.
    func testPauseCannotInterleaveBetweenValidationRenameAndCompletion() async throws {
        let root = try temporaryRoot()
        let fixture = try automaticFixture(root)
        try adoptFile(fixture.accessStore, root: root, item: "item-1", access: "access-1")
        try fixture.subscriptions.register(
            fileSubscription(item: "item-1", access: "access-1", subscription: "subscription-1")
        )
        let claim = try claimScope(fixture.scheduling)
        let coordinator = CapturePreparationCoordinator(
            accessStore: fixture.accessStore,
            queue: fixture.queue,
            reader: TestOriginalReader { try Data(contentsOf: $0) },
            fileSubscriptions: fixture.subscriptions,
            gate: fixture.gate
        )
        let started = DispatchSemaphore(value: 0)
        let paused = DispatchSemaphore(value: 0)
        let subscriptions = fixture.subscriptions
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
        }

        let published = try await coordinator.prepareAndPublish(
            [automaticItem()],
            binding: publicationBinding
        ) { entries in
            // The pause is still waiting: the consent this batch was validated
            // against, the rename and this update are one section.
            XCTAssertEqual(paused.wait(timeout: .now() + 0.5), .timedOut)
            XCTAssertEqual(subscriptions.all().first?.state, .active)
            XCTAssertEqual(fixture.queue.allEntries().count, 1)
            try fixture.scheduling.markPublished(
                self.schedulingKey,
                generation: claim.generation,
                intentRef: entries[0].intent.itemId,
                at: AutomaticSchedulingStateMachine.quietPeriodMilliseconds
            )
        }

        XCTAssertEqual(published.map { $0.intent.itemId }, ["item-1"])
        XCTAssertEqual(paused.wait(timeout: .now() + 5), .success)
        XCTAssertEqual(subscriptions.all().first?.state, .paused)
        XCTAssertTrue(try XCTUnwrap(fixture.scheduling.state(of: schedulingKey)).published)
    }

    /// Queue publication is the filesystem commit point. A scheduler persistence
    /// failure after it leaves the entry published and recoverable — it is not
    /// unpublished and not deleted — and the exact replay that records the
    /// obligation reconciles to the same batch instead of queueing a second one.
    func testCompletionFailureLeavesThePublishedEntryRecoverableAndReplayDoesNotDuplicate() async throws {
        let root = try temporaryRoot()
        let fixture = try automaticFixture(root)
        try adoptFile(fixture.accessStore, root: root, item: "item-1", access: "access-1")
        try fixture.subscriptions.register(
            fileSubscription(item: "item-1", access: "access-1", subscription: "subscription-1")
        )
        let coordinator = CapturePreparationCoordinator(
            accessStore: fixture.accessStore,
            queue: fixture.queue,
            reader: TestOriginalReader { try Data(contentsOf: $0) },
            fileSubscriptions: fixture.subscriptions,
            gate: fixture.gate
        )

        // Nothing is claimed yet, so recording the publication is refused: a real
        // scheduler failure in the window after the queue committed.
        do {
            _ = try await coordinator.prepareAndPublish(
                [automaticItem()],
                binding: publicationBinding
            ) { entries in
                try fixture.scheduling.markPublished(
                    self.schedulingKey,
                    generation: 1,
                    intentRef: entries[0].intent.itemId,
                    at: 0
                )
            }
            XCTFail("Expected the completion failure to surface")
        } catch let failure as CapturePublicationIncomplete {
            XCTAssertEqual(failure.published.map { $0.intent.itemId }, ["item-1"])
            XCTAssertEqual(failure.cause as? AutomaticSchedulingError, .notClaimed)
        }

        // Published, and recoverable: the entry, its private bytes and the closed
        // batch a fresh process rebuilds its index from are all still there.
        XCTAssertEqual(fixture.queue.allEntries().map { $0.state }, [.pending])
        XCTAssertEqual(
            try Data(contentsOf: fixture.queue.payloadURL(for: "item-1")),
            Data("body-item-1".utf8)
        )
        XCTAssertEqual(queueNames(root, in: "published").count, 1)
        XCTAssertTrue(queueNames(root, in: "candidates").isEmpty)
        let reopened = try CaptureIntentQueue(root: root.appendingPathComponent("queue"))
        XCTAssertEqual(reopened.allEntries().map { $0.intent.itemId }, ["item-1"])
        // The obligation is the only thing missing.
        XCTAssertNil(fixture.scheduling.state(of: schedulingKey))

        let claim = try claimScope(fixture.scheduling)
        let replayed = try await coordinator.prepareAndPublish(
            [automaticItem()],
            binding: publicationBinding
        ) { entries in
            try fixture.scheduling.markPublished(
                self.schedulingKey,
                generation: claim.generation,
                intentRef: entries[0].intent.itemId,
                at: AutomaticSchedulingStateMachine.quietPeriodMilliseconds
            )
        }

        // Reconciled to the batch already published: same entry, one batch, one
        // set of payload bytes.
        XCTAssertEqual(replayed, fixture.queue.allEntries())
        XCTAssertEqual(replayed.map { $0.intent.itemId }, ["item-1"])
        XCTAssertEqual(queueNames(root, in: "published").count, 1)
        XCTAssertEqual(payloadFiles(root), ["payload-item-1.payload"])
        let state = try XCTUnwrap(fixture.scheduling.state(of: schedulingKey))
        XCTAssertTrue(state.published)
        XCTAssertEqual(state.intentRef, "item-1")
    }

    /// Manual publication passes no completion and gains no obligation: it
    /// publishes exactly as it did, and the scheduler is left untouched.
    func testManualPublicationWithoutACompletionIsUnchanged() async throws {
        let root = try temporaryRoot()
        let fixture = try automaticFixture(root)
        var items: [CapturePreparationItem] = []
        for index in 1...2 {
            let item = "item-\(index)"
            let access = "access-\(index)"
            try adoptFile(fixture.accessStore, root: root, item: item, access: access)
            items.append(
                CapturePreparationItem(
                    access: NativeAccessExpectation(accessRef: access, generation: 1),
                    seed: seed(item: item, access: access)
                )
            )
        }
        let coordinator = CapturePreparationCoordinator(
            accessStore: fixture.accessStore,
            queue: fixture.queue,
            reader: TestOriginalReader { try Data(contentsOf: $0) },
            gate: fixture.gate
        )

        let published = try await coordinator.prepareAndPublish(items, binding: publicationBinding)

        XCTAssertEqual(published.map { $0.intent.itemId }, ["item-1", "item-2"])
        XCTAssertEqual(
            try Data(contentsOf: fixture.queue.payloadURL(for: "item-2")),
            Data("body-item-2".utf8)
        )
        XCTAssertEqual(fixture.queue.allEntries().count, 2)
        XCTAssertEqual(queueNames(root, in: "published").count, 1)
        XCTAssertEqual(payloadFiles(root), ["payload-item-1.payload", "payload-item-2.payload"])
        XCTAssertEqual(fixture.scheduling.trackedScopes, 0)
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

    // MARK: - AT-24 · bytes and their metadata are one version

    /// The pairing itself, at the boundary that enforces it. Preparation freezes
    /// a length and a digest from the buffer it is about to write, and the queue
    /// reproves that pair against the bytes before any of them is published, so
    /// one version's metadata over another version's payload cannot commit —
    /// which is the only way a file changing mid-read could reach Core as a
    /// coherent-looking capture.
    func testMetadataFromOneVersionOverAnotherVersionsBytesCannotCommit() throws {
        let root = try temporaryRoot()
        let queue = try makeQueue(root)
        let before = Data("the version the metadata describes".utf8)
        let after = Data("the version the bytes came from".utf8)
        XCTAssertNotEqual(before.count, after.count)
        let frozen = try CaptureIntentV3(
            submissionId: "submission-1",
            itemId: "item-1",
            targetRef: "target-1",
            workspaceRef: "workspace-1",
            targetBindingRevision: "target-binding-1",
            callerContextBinding: callerBinding,
            surface: .manual,
            contentKind: .pdf,
            payloadRef: "payload-item-1",
            byteLength: UInt64(before.count),
            localIntegrityDigest: Self.sha256(before),
            requestedSourceIdentity: "source-item-1",
            operationKey: "operation-item-1",
            intentKind: .refresh,
            basePrecondition: .expected(nil),
            processingProfile: ProcessingProfileBinding(
                profileRef: "profile-1",
                revision: "profile-revision-1"
            ),
            createdAt: "2026-10-14T00:00:00Z",
            displayName: "item-1.pdf"
        )

        do {
            _ = try queue.prepareCandidate([(intent: frozen, payload: after)])
            XCTFail("expected the mismatched pair to be refused")
        } catch {
            XCTAssertEqual(error as? CaptureQueueError, .payloadMismatch)
        }

        XCTAssertTrue(queue.allEntries().isEmpty)
        XCTAssertTrue(payloadFiles(root).isEmpty)
        // The matching pair is what does commit, so the refusal above is the
        // mismatch and nothing else.
        let candidate = try queue.prepareCandidate([(intent: frozen, payload: before)])
        XCTAssertEqual(try queue.publish(candidate).map { $0.intent.itemId }, ["item-1"])
        XCTAssertEqual(try Data(contentsOf: queue.payloadURL(for: "item-1")), before)
    }

    /// An original the reader finds changed on every attempt — the refusal
    /// `SystemCoordinatedOriginalReader` raises when its stat before the read
    /// and its stat after it disagree. It is a bounded failure after the
    /// retries, and the whole batch goes with it: the sibling the read never
    /// found a problem with publishes nothing either.
    func testAnOriginalChangingOnEveryAttemptRefusesTheWholeBatch() async throws {
        let root = try temporaryRoot()
        let accessStore = try makeAccessStore(root)
        let queue = try makeQueue(root)
        var items: [CapturePreparationItem] = []
        for index in 1...2 {
            let item = "item-\(index)"
            try adoptFile(accessStore, root: root, item: item, access: "access-\(index)")
            items.append(
                CapturePreparationItem(
                    access: NativeAccessExpectation(accessRef: "access-\(index)", generation: 1),
                    seed: seed(item: item, access: "access-\(index)")
                )
            )
        }
        let attempts = AttemptCounter()
        let reader = TestOriginalReader { url in
            guard url.lastPathComponent == "item-2.pdf" else { return try Data(contentsOf: url) }
            attempts.record()
            throw CapturePreparationError.fileChanged
        }
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: reader
        )

        do {
            _ = try await coordinator.prepareAndPublish(items, binding: publicationBinding)
            XCTFail("expected the changing original to be refused")
        } catch {
            XCTAssertEqual(error as? CapturePreparationError, .fileChanged)
        }

        XCTAssertEqual(attempts.count, 3, "the reader's retries are the bound")
        XCTAssertTrue(queue.allEntries().isEmpty)
        XCTAssertTrue(payloadFiles(root).isEmpty)
        XCTAssertEqual(queueNames(root, in: "published"), [])
        XCTAssertEqual(queueNames(root, in: "candidates"), [])
    }

    /// A file that settles publishes the attempt that settled it. The metadata
    /// comes from that attempt's bytes, not from the ones the earlier attempts
    /// were refused for.
    func testAReadThatSettlesOnARetryPublishesExactlyTheBytesItSettledOn() async throws {
        let root = try temporaryRoot()
        let accessStore = try makeAccessStore(root)
        let queue = try makeQueue(root)
        let file = try adoptFile(accessStore, root: root, item: "item-1", access: "access-1")
        let attempts = AttemptCounter()
        let reader = TestOriginalReader { url in
            // Two attempts find the file in motion; the third reads what it
            // settled on, through the production reader.
            if attempts.record() < 3 {
                try Data("mid-write-\(attempts.count)".utf8).write(to: url)
                throw CapturePreparationError.fileChanged
            }
            try Data("settled".utf8).write(to: url)
            return try SystemCoordinatedOriginalReader()
                .readStableBytes(from: url, maximumBytes: CaptureIntentV3.maximumPayloadBytes)
        }
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: reader
        )

        let published = try await coordinator.prepareAndPublish(
            [
                CapturePreparationItem(
                    access: NativeAccessExpectation(accessRef: "access-1", generation: 1),
                    seed: seed(item: "item-1", access: "access-1")
                ),
            ],
            binding: publicationBinding
        )

        let entry = try XCTUnwrap(published.first)
        let payload = try Data(contentsOf: queue.payloadURL(for: "item-1"))
        XCTAssertEqual(payload, Data("settled".utf8))
        XCTAssertEqual(payload, try Data(contentsOf: file))
        XCTAssertEqual(entry.intent.byteLength, UInt64(payload.count))
        XCTAssertEqual(entry.intent.localIntegrityDigest, Self.sha256(payload))
    }

    // MARK: - AT-24 · a real file mutated while the production reader has it open

    /// The production `SystemCoordinatedOriginalReader`, over a real file that is
    /// rewritten in place after the first chunk was read and before the closing
    /// stat — the window where one version's head and another's tail would
    /// otherwise be stitched into one payload. Same length, so only the
    /// timestamp can tell. The attempt is refused and the read that settles
    /// publishes one whole version, with the length and digest of that version.
    func testAFileRewrittenWhileTheProductionReaderHasItOpenIsNeverCapturedAsAMix() async throws {
        let root = try temporaryRoot()
        let accessStore = try makeAccessStore(root)
        let queue = try makeQueue(root)
        let file = try adoptFile(accessStore, root: root, item: "item-1", access: "access-1")
        let length = 2_500_000
        let first = Data(repeating: 0x41, count: length)
        let second = Data(repeating: 0x42, count: length)
        try first.write(to: file)
        let chunks = AttemptCounter()
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: SystemCoordinatedOriginalReader(afterChunk: { url in
                // Only the first chunk of the first attempt.
                guard chunks.record() == 1 else { return }
                let handle = try? FileHandle(forWritingTo: url)
                try? handle?.write(contentsOf: second)
                try? handle?.close()
            })
        )

        let published = try await coordinator.prepareAndPublish(
            [
                CapturePreparationItem(
                    access: NativeAccessExpectation(accessRef: "access-1", generation: 1),
                    seed: seed(item: "item-1", access: "access-1")
                ),
            ],
            binding: publicationBinding
        )

        // Three chunks per pass: the first pass was refused and the second was
        // the one that settled, so the rewrite really did land mid-read.
        XCTAssertEqual(chunks.count, 6)
        XCTAssertEqual(try Data(contentsOf: file), second)
        let entry = try XCTUnwrap(published.first)
        let payload = try Data(contentsOf: queue.payloadURL(for: "item-1"))
        XCTAssertEqual(payload, second, "one whole version, never head of one and tail of the other")
        XCTAssertFalse(payload.contains(0x41))
        XCTAssertEqual(entry.intent.byteLength, UInt64(second.count))
        XCTAssertEqual(entry.intent.localIntegrityDigest, Self.sha256(second))
    }

    /// Growth is the other way a read can pair a stale length with fresh bytes.
    /// The pass that saw the file grow is refused; the one that settles carries
    /// the grown payload and the grown length together.
    func testAFileThatGrowsWhileTheProductionReaderHasItOpenPublishesLengthAndBytesTogether() async throws {
        let root = try temporaryRoot()
        let accessStore = try makeAccessStore(root)
        let queue = try makeQueue(root)
        let file = try adoptFile(accessStore, root: root, item: "item-1", access: "access-1")
        let before = Data(repeating: 0x41, count: 1_500_000)
        try before.write(to: file)
        let chunks = AttemptCounter()
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: SystemCoordinatedOriginalReader(afterChunk: { url in
                guard chunks.record() == 1 else { return }
                let handle = try? FileHandle(forWritingTo: url)
                _ = try? handle?.seekToEnd()
                try? handle?.write(contentsOf: Data("appended".utf8))
                try? handle?.close()
            })
        )

        let published = try await coordinator.prepareAndPublish(
            [
                CapturePreparationItem(
                    access: NativeAccessExpectation(accessRef: "access-1", generation: 1),
                    seed: seed(item: "item-1", access: "access-1")
                ),
            ],
            binding: publicationBinding
        )

        let grown = before + Data("appended".utf8)
        XCTAssertEqual(chunks.count, 4, "two chunks a pass, and the first pass was refused")
        let entry = try XCTUnwrap(published.first)
        XCTAssertEqual(try Data(contentsOf: queue.payloadURL(for: "item-1")), grown)
        XCTAssertEqual(entry.intent.byteLength, UInt64(grown.count))
        XCTAssertEqual(entry.intent.localIntegrityDigest, Self.sha256(grown))
    }

    /// A file that is rewritten on every chunk of every pass never settles. The
    /// reader's three attempts are the bound, the failure is `fileChanged`, and
    /// nothing is published — no entry, no payload, no candidate.
    func testAFileRewrittenOnEveryReadIsABoundedFailureThatPublishesNothing() async throws {
        let root = try temporaryRoot()
        let accessStore = try makeAccessStore(root)
        let queue = try makeQueue(root)
        let file = try adoptFile(accessStore, root: root, item: "item-1", access: "access-1")
        let length = 2_500_000
        try Data(repeating: 0x30, count: length).write(to: file)
        let chunks = AttemptCounter()
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: SystemCoordinatedOriginalReader(afterChunk: { url in
                let next = UInt8(0x31 + chunks.record() % 9)
                let handle = try? FileHandle(forWritingTo: url)
                try? handle?.write(contentsOf: Data(repeating: next, count: length))
                try? handle?.close()
            })
        )

        do {
            _ = try await coordinator.prepareAndPublish(
                [
                    CapturePreparationItem(
                        access: NativeAccessExpectation(accessRef: "access-1", generation: 1),
                        seed: seed(item: "item-1", access: "access-1")
                    ),
                ],
                binding: publicationBinding
            )
            XCTFail("expected a file that never holds still to be refused")
        } catch {
            XCTAssertEqual(error as? CapturePreparationError, .fileChanged)
        }

        XCTAssertEqual(chunks.count, 9, "three chunks a pass, three passes, then it stops")
        XCTAssertTrue(queue.allEntries().isEmpty)
        XCTAssertTrue(payloadFiles(root).isEmpty)
        XCTAssertEqual(queueNames(root, in: "published"), [])
        XCTAssertEqual(queueNames(root, in: "candidates"), [])
    }

    // MARK: - AT-27 · a Locate that wins takes the whole batch

    /// A confirmed Locate of one grant, landing after every original was read and before
    /// the batch commits. The reserved generation wins: the stale batch is refused whole,
    /// including the item whose own grant nobody touched, and the file that was chosen is
    /// neither published nor resolved. The route holds no grant until Core confirms.
    func testALocateRacingTheFinalPublicationPreventsTheEntireBatch() async throws {
        let root = try temporaryRoot()
        let gate = PublicationGate()
        let accessStore = try NativeAccessStore(
            stateDirectory: root.appendingPathComponent("access"),
            codec: PreparationBookmarkCodec(),
            gate: gate
        )
        let queue = try CaptureIntentQueue(root: root.appendingPathComponent("queue"), gate: gate)
        var items: [CapturePreparationItem] = []
        for index in 1...2 {
            let item = "item-\(index)"
            try adoptFile(accessStore, root: root, item: item, access: "access-\(index)")
            items.append(
                CapturePreparationItem(
                    access: NativeAccessExpectation(accessRef: "access-\(index)", generation: 1),
                    seed: seed(item: item, access: "access-\(index)")
                )
            )
        }
        // Both grants are published work's, which is what a Locate can move.
        try accessStore.activateGrants(accessRefs: ["access-1", "access-2"])
        let chosen = root.appendingPathComponent("chosen.pdf")
        try Data("the file Locate chose".utf8).write(to: chosen)
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: TestOriginalReader { try Data(contentsOf: $0) },
            gate: gate
        )
        let candidatesAtLocate = NamesBox()
        coordinator.afterCandidatePrepared = {
            candidatesAtLocate.names = self.queueNames(root, in: "candidates")
            _ = try? accessStore.reserveRelink(
                accessRef: "access-1",
                selectedURL: chosen,
                binding: NativeRootAccessBinding(publicationBinding)
            )
        }

        do {
            _ = try await coordinator.prepareAndPublish(items, binding: publicationBinding)
            XCTFail("expected the reserved generation to prevent the batch")
        } catch {
            XCTAssertEqual(error as? NativeAccessError, .relinkPending)
        }

        XCTAssertEqual(
            candidatesAtLocate.names.count, 1,
            "the Locate landed with the complete batch prepared and unpublished"
        )
        XCTAssertTrue(queue.allEntries().isEmpty)
        XCTAssertTrue(payloadFiles(root).isEmpty)
        XCTAssertEqual(queueNames(root, in: "published"), [])
        XCTAssertEqual(queueNames(root, in: "candidates"), [], "the refused candidate is discarded")
        XCTAssertEqual(
            accessStore.descriptors().map { "\($0.accessRef)=\($0.state)@\($0.generation)" },
            ["access-1=relinking@2", "access-2=connected@1"]
        )
        // The reservation is durable and the refused batch undid nothing.
        let reopened = try NativeAccessStore(
            stateDirectory: root.appendingPathComponent("access"),
            codec: PreparationBookmarkCodec()
        )
        XCTAssertEqual(
            reopened.descriptors().map { "\($0.accessRef)=\($0.state)@\($0.generation)" },
            ["access-1=relinking@2", "access-2=connected@1"]
        )
        XCTAssertEqual(reopened.pendingOriginalRelinks().map(\.generation), [2])
    }

    // MARK: - AT-28 · a Disconnect that wins takes the whole batch

    /// A real Disconnect of one grant, landing after every original was read and
    /// before the batch commits. The revocation wins, and it prevents the whole
    /// stale batch — including the item whose own grant nobody touched.
    func testADisconnectRacingTheFinalPublicationPreventsTheEntireBatch() async throws {
        let root = try temporaryRoot()
        let gate = PublicationGate()
        let accessStore = try NativeAccessStore(
            stateDirectory: root.appendingPathComponent("access"),
            codec: PreparationBookmarkCodec(),
            gate: gate
        )
        let queue = try CaptureIntentQueue(root: root.appendingPathComponent("queue"), gate: gate)
        var items: [CapturePreparationItem] = []
        for index in 1...2 {
            let item = "item-\(index)"
            try adoptFile(accessStore, root: root, item: item, access: "access-\(index)")
            items.append(
                CapturePreparationItem(
                    access: NativeAccessExpectation(accessRef: "access-\(index)", generation: 1),
                    seed: seed(item: item, access: "access-\(index)")
                )
            )
        }
        // The person disconnects the first original while the last one is still
        // being read: both are already prepared, neither has published.
        let reader = TestOriginalReader { url in
            let data = try Data(contentsOf: url)
            if url.lastPathComponent == "item-2.pdf" {
                _ = try accessStore.disconnect(accessRef: "access-1")
            }
            return data
        }
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: reader,
            gate: gate
        )

        do {
            _ = try await coordinator.prepareAndPublish(items, binding: publicationBinding)
            XCTFail("expected the revocation to prevent the batch")
        } catch {
            XCTAssertEqual(error as? NativeAccessError, .disconnected)
        }

        XCTAssertTrue(queue.allEntries().isEmpty)
        XCTAssertTrue(payloadFiles(root).isEmpty)
        XCTAssertEqual(queueNames(root, in: "published"), [])
        XCTAssertEqual(queueNames(root, in: "candidates"), [])
        // The untouched grant is still the person's, and the revoked one stays
        // revoked: a refused batch undoes neither.
        XCTAssertEqual(
            accessStore.descriptors().map { "\($0.accessRef)=\($0.state)" },
            ["access-1=disconnected", "access-2=connected"]
        )
    }

    /// The other side of the same race. A Disconnect raised once the batch is
    /// inside the publication critical section blocks on the shared gate, so it
    /// lands after the commit rather than half way through it.
    func testADisconnectRaisedInsideTheCriticalSectionCannotSplitTheBatch() async throws {
        let root = try temporaryRoot()
        let gate = PublicationGate()
        let accessStore = try NativeAccessStore(
            stateDirectory: root.appendingPathComponent("access"),
            codec: PreparationBookmarkCodec(),
            gate: gate
        )
        let queue = try CaptureIntentQueue(root: root.appendingPathComponent("queue"), gate: gate)
        var items: [CapturePreparationItem] = []
        for index in 1...2 {
            let item = "item-\(index)"
            try adoptFile(accessStore, root: root, item: item, access: "access-\(index)")
            items.append(
                CapturePreparationItem(
                    access: NativeAccessExpectation(accessRef: "access-\(index)", generation: 1),
                    seed: seed(item: item, access: "access-\(index)")
                )
            )
        }
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: TestOriginalReader { try Data(contentsOf: $0) },
            gate: gate
        )
        let started = DispatchSemaphore(value: 0)
        let revoked = DispatchSemaphore(value: 0)
        coordinator.afterConsentValidation = {
            Thread.detachNewThread {
                started.signal()
                _ = try? accessStore.disconnect(accessRef: "access-1")
                revoked.signal()
            }
            started.wait()
            XCTAssertEqual(
                revoked.wait(timeout: .now() + 0.5),
                .timedOut,
                "the Disconnect must block on the publication gate"
            )
        }

        let published = try await coordinator.prepareAndPublish(items, binding: publicationBinding)

        XCTAssertEqual(published.map { $0.intent.itemId }, ["item-1", "item-2"])
        XCTAssertEqual(revoked.wait(timeout: .now() + 5), .success)
        XCTAssertEqual(queue.allEntries().count, 2)
        XCTAssertEqual(
            accessStore.descriptors().map { "\($0.accessRef)=\($0.state)" },
            ["access-1=disconnected", "access-2=connected"]
        )
    }

    /// The ordering the gate names, with nothing between it and the commit. The
    /// whole batch is written and verified as a queue candidate, and only then
    /// does the person's source-level Disconnect land — through the store's own
    /// document-level revocation, the one that takes every route for the
    /// document. The revocation wins, so the stale batch is refused whole: the
    /// item whose document nobody touched publishes nothing either.
    func testADisconnectThatWinsAfterTheCandidateIsPreparedPublishesNoneOfTheBatch() async throws {
        let root = try temporaryRoot()
        let gate = PublicationGate()
        let accessStore = try NativeAccessStore(
            stateDirectory: root.appendingPathComponent("access"),
            codec: PreparationBookmarkCodec(),
            gate: gate
        )
        let queue = try CaptureIntentQueue(root: root.appendingPathComponent("queue"), gate: gate)
        var items: [CapturePreparationItem] = []
        for index in 1...2 {
            let item = "item-\(index)"
            try adoptFile(accessStore, root: root, item: item, access: "access-\(index)")
            items.append(
                CapturePreparationItem(
                    access: NativeAccessExpectation(accessRef: "access-\(index)", generation: 1),
                    seed: seed(item: item, access: "access-\(index)")
                )
            )
        }
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: TestOriginalReader { try Data(contentsOf: $0) },
            gate: gate
        )
        let candidatesAtDisconnect = NamesBox()
        coordinator.afterCandidatePrepared = {
            candidatesAtDisconnect.names = self.queueNames(root, in: "candidates")
            _ = try? accessStore.disconnect(linkedDocumentID: "document-item-1")
        }

        do {
            _ = try await coordinator.prepareAndPublish(items, binding: publicationBinding)
            XCTFail("expected the revocation to prevent the batch")
        } catch {
            XCTAssertEqual(error as? NativeAccessError, .disconnected)
        }

        XCTAssertEqual(
            candidatesAtDisconnect.names.count, 1,
            "the Disconnect landed with the complete batch prepared and unpublished"
        )
        XCTAssertTrue(queue.allEntries().isEmpty)
        XCTAssertTrue(payloadFiles(root).isEmpty)
        XCTAssertEqual(queueNames(root, in: "published"), [])
        XCTAssertEqual(queueNames(root, in: "candidates"), [], "the refused candidate is discarded")
        XCTAssertEqual(
            accessStore.descriptors().map { "\($0.accessRef)=\($0.state)" },
            ["access-1=disconnected", "access-2=connected"]
        )
        // The revocation is durable and the refused batch undid nothing.
        let reopened = try NativeAccessStore(
            stateDirectory: root.appendingPathComponent("access"),
            codec: PreparationBookmarkCodec()
        )
        XCTAssertEqual(
            reopened.descriptors().map { "\($0.accessRef)=\($0.state)" },
            ["access-1=disconnected", "access-2=connected"]
        )
    }

    private static func sha256(_ payload: Data) -> String {
        "sha256:" + SHA256.hash(data: payload).map { String(format: "%02x", $0) }.joined()
    }
}

/// Counts the reader's attempts across the detached preparation task.
private final class AttemptCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var attempts = 0

    var count: Int { lock.withLock { attempts } }

    @discardableResult
    func record() -> Int { lock.withLock { attempts += 1; return attempts } }
}

/// Carries a name list out of a seam that runs on the preparation thread.
private final class NamesBox: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: [String] = []

    var names: [String] {
        get { lock.withLock { stored } }
        set { lock.withLock { stored = newValue } }
    }
}
