import CryptoKit
import Foundation
import XCTest
@testable import DocumentIntakeMacOS

/// Carries a candidate prepared on a competing thread back to the test body.
private final class CandidateBox: @unchecked Sendable {
    var candidate: CaptureQueueCandidate?
}

final class CaptureIntentQueueTests: XCTestCase {
    private func names(_ root: URL, in directory: String) -> [String] {
        ((try? FileManager.default.contentsOfDirectory(
            atPath: root.appendingPathComponent(directory, isDirectory: true).path
        )) ?? []).sorted()
    }

    private func publishedBatch(_ root: URL) throws -> URL {
        let published = root.appendingPathComponent("published", isDirectory: true)
        let batches = names(root, in: "published")
        XCTAssertEqual(batches.count, 1)
        return published.appendingPathComponent(try XCTUnwrap(batches.first), isDirectory: true)
    }

    private func bytesOnDisk(_ directory: URL) -> UInt64 {
        guard let children = FileManager.default.enumerator(
            at: directory,
            includingPropertiesForKeys: [.fileSizeKey, .isRegularFileKey]
        ) else { return 0 }
        var total: UInt64 = 0
        for case let url as URL in children {
            let values = try? url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey])
            guard values?.isRegularFile == true, let size = values?.fileSize else { continue }
            total += UInt64(size)
        }
        return total
    }

    private func temporaryQueue() throws -> URL {
        let url = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("omnivia-capture-queue-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    private func digest(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func intent(
        item: String = "item-1",
        payload: Data,
        kind: CaptureIntentKind = .newCapture,
        base: CaptureBasePrecondition = .create,
        surface: CaptureSurface = .manual,
        original: OriginalBindingIntent? = nil,
        consent: ConsentBinding? = nil,
        bulk: BulkOriginBinding? = nil
    ) throws -> CaptureIntentV3 {
        try CaptureIntentV3(
            submissionId: "submission-1",
            itemId: item,
            targetRef: "target-1",
            workspaceRef: "ws-1",
            targetBindingRevision: "target-revision-1",
            callerContextBinding: CallerContextBinding(
                clientContextRef: "client-1",
                principalRef: "principal-1",
                contextRevision: "context-1"
            ),
            surface: surface,
            contentKind: .pdf,
            payloadRef: "payload-" + item,
            byteLength: UInt64(payload.count),
            localIntegrityDigest: digest(payload),
            requestedSourceIdentity: "source-" + item,
            operationKey: "operation-" + item,
            intentKind: kind,
            basePrecondition: base,
            processingProfile: ProcessingProfileBinding(
                profileRef: "profile-1",
                revision: "profile-revision-1"
            ),
            originalBinding: original,
            consent: consent,
            bulkOrigin: bulk,
            createdAt: "2026-10-14T00:00:00Z",
            displayName: "Document.pdf"
        )
    }

    func testCreationOmitsBaseWhileRefreshPreservesExplicitNull() throws {
        let payload = Data("pdf".utf8)
        let creation = try intent(payload: payload)
        let refresh = try intent(
            item: "item-2",
            payload: payload,
            kind: .refresh,
            base: .expected(nil)
        )
        let creationJSON = try XCTUnwrap(
            JSONSerialization.jsonObject(with: creation.canonicalData()) as? [String: Any]
        )
        let refreshJSON = try XCTUnwrap(
            JSONSerialization.jsonObject(with: refresh.canonicalData()) as? [String: Any]
        )

        XCTAssertNil(creationJSON["expectedSuccessfulCaptureId"])
        XCTAssertTrue(refreshJSON.keys.contains("expectedSuccessfulCaptureId"))
        XCTAssertTrue(refreshJSON["expectedSuccessfulCaptureId"] is NSNull)
        XCTAssertEqual(try JSONDecoder().decode(CaptureIntentV3.self, from: refresh.canonicalData()), refresh)
    }

    func testIntentRejectsMissingAutomaticConsentAndIncompleteRootLineage() throws {
        let payload = Data("pdf".utf8)
        XCTAssertThrowsError(
            try intent(
                payload: payload,
                kind: .automaticCapture,
                base: .expected(nil),
                surface: .automaticFile
            )
        ) { XCTAssertEqual($0 as? CaptureIntentError, .invalidLineage) }
        let incomplete = OriginalBindingIntent(
            accessRecordId: "access-1",
            bindingGeneration: 1,
            rootAccessGroupId: "root-1"
        )
        XCTAssertThrowsError(
            try intent(payload: payload, original: incomplete)
        ) { XCTAssertEqual($0 as? CaptureIntentError, .invalidLineage) }
    }

    func testFolderImportCarriesExplicitRootAndBulkLineageWithoutStandingConsent() throws {
        let payload = Data("pdf".utf8)
        let original = OriginalBindingIntent(
            accessRecordId: "access-1",
            bindingGeneration: 2,
            rootAccessGroupId: "root-1",
            rootBindingGeneration: 3,
            memberId: "member-1",
            membershipGeneration: 4
        )
        let bulk = BulkOriginBinding(
            bulkRunId: "run-1",
            manifestRevision: "manifest-1",
            memberId: "member-1"
        )

        let frozen = try intent(
            payload: payload,
            kind: .folderCapture,
            base: .expected(nil),
            surface: .folderImport,
            original: original,
            bulk: bulk
        )

        XCTAssertNil(frozen.consent)
        XCTAssertEqual(frozen.bulkOrigin, bulk)
    }

    func testAutomaticFileRequiresMatchingKindBindingAndStandingConsent() throws {
        let payload = Data("pdf".utf8)
        let original = OriginalBindingIntent(accessRecordId: "access-1", bindingGeneration: 2)
        let consent = ConsentBinding(
            subscriptionId: "subscription-1",
            consentGeneration: 3,
            configGeneration: 4,
            dirtyEpoch: 5
        )

        let frozen = try intent(
            payload: payload,
            kind: .automaticCapture,
            base: .expected(nil),
            surface: .automaticFile,
            original: original,
            consent: consent
        )

        XCTAssertEqual(frozen.consent, consent)
        XCTAssertThrowsError(
            try intent(
                item: "item-2",
                payload: payload,
                kind: .refresh,
                base: .expected(nil),
                surface: .automaticFile,
                original: original,
                consent: consent
            )
        ) { XCTAssertEqual($0 as? CaptureIntentError, .invalidLineage) }
    }

    func testIntentDecoderRejectsUnknownTopLevelFields() throws {
        let payload = Data("pdf".utf8)
        let valid = try intent(payload: payload)
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: valid.canonicalData()) as? [String: Any]
        )
        object["futureAuthority"] = true
        let changed = try JSONSerialization.data(withJSONObject: object)

        XCTAssertThrowsError(try JSONDecoder().decode(CaptureIntentV3.self, from: changed)) {
            XCTAssertEqual($0 as? CaptureIntentError, .invalidShape)
        }
    }

    func testQueuePersistsExactIntentAndPayloadAndReplaysExactEnqueue() throws {
        let root = try temporaryQueue()
        let payload = Data("pdf-body".utf8)
        let frozen = try intent(payload: payload)
        let queue = try CaptureIntentQueue(root: root)

        let first = try queue.enqueue(intent: frozen, payload: payload)
        let replay = try queue.enqueue(intent: frozen, payload: payload)
        let reopened = try CaptureIntentQueue(root: root)

        XCTAssertEqual(first, replay)
        XCTAssertEqual(reopened.allEntries(), [first])
        XCTAssertEqual(try Data(contentsOf: reopened.payloadURL(for: "item-1")), payload)
        let manifest = root.appendingPathComponent("capture-queue-v1.json")
        let mode = try FileManager.default.attributesOfItem(atPath: manifest.path)[.posixPermissions] as? NSNumber
        XCTAssertEqual(mode?.intValue, 0o600)
        let document = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: manifest)) as? [String: Any]
        )
        XCTAssertEqual(document["schema"] as? String, "omnivia.macos.capture-queue.v2")
    }

    func testUnknownOutcomeRequiresExactKnownNoEffectEvidenceBeforeRetry() throws {
        let root = try temporaryQueue()
        let payload = Data("pdf-body".utf8)
        let queue = try CaptureIntentQueue(root: root)
        _ = try queue.enqueue(intent: intent(payload: payload), payload: payload)
        let request = digest(Data("request".utf8))
        let descriptor = digest(Data("descriptor".utf8))
        let staging = try queue.beginStage(
            itemID: "item-1",
            requestDigest: request,
            descriptorDigest: descriptor
        )
        _ = try queue.markUnknownOutcome(
            itemID: "item-1",
            attemptGeneration: staging.attemptGeneration
        )

        XCTAssertThrowsError(
            try queue.recoverKnownNoEffect(
                itemID: "item-1",
                attemptGeneration: staging.attemptGeneration,
                requestDigest: digest(Data("other".utf8)),
                descriptorDigest: descriptor
            )
        ) { XCTAssertEqual($0 as? CaptureQueueError, .invalidTransition) }
        let recovered = try queue.recoverKnownNoEffect(
            itemID: "item-1",
            attemptGeneration: staging.attemptGeneration,
            requestDigest: request,
            descriptorDigest: descriptor
        )
        XCTAssertEqual(recovered.state, .pending)
        XCTAssertEqual(recovered.intent.operationKey, "operation-item-1")
        XCTAssertEqual(recovered.attempts.map(\.disposition), [.knownNoEffect])

        let replacement = try queue.beginStage(
            itemID: "item-1",
            requestDigest: digest(Data("replacement-request".utf8)),
            descriptorDigest: descriptor
        )
        XCTAssertEqual(replacement.attemptGeneration, 2)
        XCTAssertEqual(replacement.attempts.map(\.generation), [1, 2])
        XCTAssertEqual(replacement.attempts.map(\.disposition), [.knownNoEffect, .active])
        XCTAssertNotEqual(replacement.attempts[0].operationKey, replacement.attempts[1].operationKey)

        let reopened = try CaptureIntentQueue(root: root)
        XCTAssertEqual(reopened.allEntries()[0].attempts.map(\.generation), [1, 2])
        XCTAssertEqual(reopened.allEntries()[0].attempts.map(\.disposition), [.knownNoEffect, .unknown])
    }

    func testLegacyV1InterruptedAttemptMigratesWithoutInventingNewIdentity() throws {
        let root = try temporaryQueue()
        let payload = Data("pdf-body".utf8)
        do {
            let queue = try CaptureIntentQueue(root: root)
            _ = try queue.enqueue(intent: intent(payload: payload), payload: payload)
            _ = try queue.beginStage(
                itemID: "item-1",
                requestDigest: digest(Data("request".utf8)),
                descriptorDigest: digest(Data("descriptor".utf8))
            )
        }
        let manifest = root.appendingPathComponent("capture-queue-v1.json")
        var document = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: manifest)) as? [String: Any]
        )
        document["schema"] = "omnivia.macos.capture-queue.v1"
        var entries = try XCTUnwrap(document["entries"] as? [[String: Any]])
        entries[0].removeValue(forKey: "attempts")
        document["entries"] = entries
        try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys]).write(to: manifest)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: manifest.path)

        let reopened = try CaptureIntentQueue(root: root)
        let recovered = try XCTUnwrap(reopened.allEntries().first)
        XCTAssertEqual(recovered.state, .unknownOutcome)
        XCTAssertEqual(recovered.attempts.count, 1)
        XCTAssertEqual(recovered.attempts[0].generation, 1)
        XCTAssertEqual(recovered.attempts[0].disposition, .unknown)
    }

    func testFutureQueueSchemaIsRejectedWithoutRewritingBytes() throws {
        let root = try temporaryQueue()
        let payload = Data("pdf-body".utf8)
        do {
            let queue = try CaptureIntentQueue(root: root)
            _ = try queue.enqueue(intent: intent(payload: payload), payload: payload)
        }
        let manifest = root.appendingPathComponent("capture-queue-v1.json")
        var document = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: manifest)) as? [String: Any]
        )
        document["schema"] = "omnivia.macos.capture-queue.v3"
        let futureBytes = try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
        try futureBytes.write(to: manifest)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: manifest.path)

        XCTAssertThrowsError(try CaptureIntentQueue(root: root)) {
            XCTAssertEqual($0 as? CaptureQueueError, .incompatibleSchema)
        }
        XCTAssertEqual(try Data(contentsOf: manifest), futureBytes)
    }

    func testRestartTurnsInterruptedStageIntoUnknownInsteadOfBlindRetry() throws {
        let root = try temporaryQueue()
        let payload = Data("pdf-body".utf8)
        do {
            let queue = try CaptureIntentQueue(root: root)
            _ = try queue.enqueue(intent: intent(payload: payload), payload: payload)
            _ = try queue.beginStage(
                itemID: "item-1",
                requestDigest: digest(Data("request".utf8)),
                descriptorDigest: digest(Data("descriptor".utf8))
            )
        }

        let reopened = try CaptureIntentQueue(root: root)

        XCTAssertEqual(reopened.allEntries().first?.state, .unknownOutcome)
    }

    func testAdmittedOutcomeRemovesPrivatePayloadButKeepsReceiptIdentity() throws {
        let root = try temporaryQueue()
        let payload = Data("pdf-body".utf8)
        let queue = try CaptureIntentQueue(root: root)
        _ = try queue.enqueue(intent: intent(payload: payload), payload: payload)
        let staging = try queue.beginStage(
            itemID: "item-1",
            requestDigest: digest(Data("request".utf8)),
            descriptorDigest: digest(Data("descriptor".utf8))
        )
        let payloadURL = try queue.payloadURL(for: "item-1")

        let admitted = try queue.markAdmitted(
            itemID: "item-1",
            attemptGeneration: staging.attemptGeneration,
            captureID: "dcap-" + String(repeating: "a", count: 64)
        )

        XCTAssertEqual(admitted.state, .admitted)
        XCTAssertEqual(admitted.intent.operationKey, "operation-item-1")
        XCTAssertFalse(FileManager.default.fileExists(atPath: payloadURL.path))
    }

    func testFailureBeforePublicationLeavesNothingAndCleansTheCandidateOnRestart() throws {
        let root = try temporaryQueue()
        let payload = Data("pdf-body".utf8)
        do {
            let queue = try CaptureIntentQueue(root: root)
            // The bytes are durable, and nothing has been published.
            let candidate = try queue.prepareCandidate([(try intent(payload: payload), payload)])
            XCTAssertEqual(candidate.itemIds, ["item-1"])
            XCTAssertEqual(names(root, in: "candidates").count, 1)
            XCTAssertTrue(names(root, in: "published").isEmpty)
            XCTAssertTrue(queue.allEntries().isEmpty)
            // The producer dies here: no publication, no discard.
        }

        let reopened = try CaptureIntentQueue(root: root)

        XCTAssertTrue(reopened.allEntries().isEmpty)
        XCTAssertTrue(names(root, in: "candidates").isEmpty)
        XCTAssertTrue(names(root, in: "published").isEmpty)
    }

    func testLostIndexAcknowledgementRecoversBothItemsOfTheBatchOnRestart() throws {
        let root = try temporaryQueue()
        let first = Data("pdf-one".utf8)
        let second = Data("pdf-two".utf8)
        let one = try intent(item: "item-1", payload: first)
        let two = try intent(item: "item-2", payload: second)
        do {
            let queue = try CaptureIntentQueue(root: root)
            queue.afterPublishRename = { throw CaptureQueueError.stateUnavailable }
            XCTAssertThrowsError(try queue.enqueueBatch([(one, first), (two, second)])) {
                XCTAssertEqual($0 as? CaptureQueueError, .stateUnavailable)
            }
            // The rename landed; nothing acknowledged it, so nothing is exposed.
            XCTAssertTrue(queue.allEntries().isEmpty)
        }
        XCTAssertEqual(names(root, in: "published").count, 1)

        let reopened = try CaptureIntentQueue(root: root)

        XCTAssertEqual(reopened.allEntries().map { $0.intent.itemId }, ["item-1", "item-2"])
        XCTAssertEqual(reopened.allEntries().map(\.state), [.pending, .pending])
        XCTAssertEqual(try Data(contentsOf: reopened.payloadURL(for: "item-1")), first)
        XCTAssertEqual(try Data(contentsOf: reopened.payloadURL(for: "item-2")), second)
    }

    func testRefusedTwoItemBatchExposesNeitherItem() throws {
        let root = try temporaryQueue()
        let first = Data("pdf-one".utf8)
        let second = Data("pdf-two".utf8)
        let queue = try CaptureIntentQueue(root: root)
        let candidate = try queue.prepareCandidate([
            (try intent(item: "item-1", payload: first), first),
            (try intent(item: "item-2", payload: second), second),
        ])
        queue.ownedBytesLimit = 0

        XCTAssertThrowsError(try queue.publish(candidate)) {
            XCTAssertEqual($0 as? CaptureQueueError, .capacityExceeded)
        }

        XCTAssertTrue(queue.allEntries().isEmpty)
        XCTAssertTrue(names(root, in: "published").isEmpty)
        queue.discard(candidate)
        XCTAssertTrue(try CaptureIntentQueue(root: root).allEntries().isEmpty)
    }

    func testTamperedPublishedBatchFailsClosed() throws {
        let payloadName = "payload-item-1.payload"
        let tamperings: [(name: String, change: (URL) throws -> Void)] = [
            ("corrupt manifest", { batch in
                try Data("{".utf8).write(to: batch.appendingPathComponent("batch.json"))
            }),
            ("missing payload", { batch in
                try FileManager.default.removeItem(at: batch.appendingPathComponent(payloadName))
            }),
            ("truncated payload", { batch in
                try Data().write(to: batch.appendingPathComponent(payloadName))
            }),
            ("same-length corrupt payload", { batch in
                try Data("pdf-bodz".utf8).write(to: batch.appendingPathComponent(payloadName))
            }),
            ("symlinked payload", { batch in
                let payload = batch.appendingPathComponent(payloadName)
                // Outside the published namespace, so only the link is tampering.
                let elsewhere = batch
                    .deletingLastPathComponent()
                    .deletingLastPathComponent()
                    .appendingPathComponent("elsewhere.bin")
                try FileManager.default.moveItem(at: payload, to: elsewhere)
                try FileManager.default.createSymbolicLink(at: payload, withDestinationURL: elsewhere)
            }),
            ("unexpected member", { batch in
                try Data("x".utf8).write(to: batch.appendingPathComponent("payload-item-9.payload"))
            }),
            ("rewritten batch digest", { batch in
                let manifest = batch.appendingPathComponent("batch.json")
                var document = try XCTUnwrap(
                    JSONSerialization.jsonObject(with: Data(contentsOf: manifest)) as? [String: Any]
                )
                document["batchDigest"] = "sha256:" + String(repeating: "f", count: 64)
                try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
                    .write(to: manifest)
            }),
        ]
        for tampering in tamperings {
            let root = try temporaryQueue()
            let payload = Data("pdf-body".utf8)
            do {
                let queue = try CaptureIntentQueue(root: root)
                _ = try queue.enqueue(intent: intent(payload: payload), payload: payload)
            }
            try tampering.change(try publishedBatch(root))

            XCTAssertThrowsError(try CaptureIntentQueue(root: root), tampering.name) {
                XCTAssertEqual($0 as? CaptureQueueError, .stateUnavailable, tampering.name)
            }
        }
    }

    func testUnsupportedNewerBatchSchemaIsPreservedByteForByte() throws {
        let root = try temporaryQueue()
        let payload = Data("pdf-body".utf8)
        do {
            let queue = try CaptureIntentQueue(root: root)
            _ = try queue.enqueue(intent: intent(payload: payload), payload: payload)
        }
        let manifest = try publishedBatch(root).appendingPathComponent("batch.json")
        var document = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: manifest)) as? [String: Any]
        )
        document["schema"] = "omnivia.macos.capture-batch.v2"
        let futureBytes = try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
        try futureBytes.write(to: manifest)

        XCTAssertThrowsError(try CaptureIntentQueue(root: root)) {
            XCTAssertEqual($0 as? CaptureQueueError, .incompatibleSchema)
        }
        XCTAssertEqual(try Data(contentsOf: manifest), futureBytes)
    }

    func testExactReplayReusesThePublishedBatchWhileConflictingReplayRefuses() throws {
        let root = try temporaryQueue()
        let payload = Data("pdf-body".utf8)
        let queue = try CaptureIntentQueue(root: root)
        let one = try intent(item: "item-1", payload: payload)
        let two = try intent(item: "item-2", payload: payload)
        let published = try queue.enqueueBatch([(one, payload), (two, payload)])

        // Same submission and items, handed over in the other order.
        let replay = try queue.enqueueBatch([(two, payload), (one, payload)])

        XCTAssertEqual(published.map { $0.intent.itemId }, ["item-1", "item-2"])
        XCTAssertEqual(replay.map { $0.intent.itemId }, ["item-2", "item-1"])
        XCTAssertEqual(
            Dictionary(uniqueKeysWithValues: replay.map { ($0.intent.itemId, $0) }),
            Dictionary(uniqueKeysWithValues: published.map { ($0.intent.itemId, $0) })
        )
        XCTAssertEqual(names(root, in: "published").count, 1)
        // A different item set naming an already published item is a different batch.
        XCTAssertThrowsError(try queue.enqueueBatch([(one, payload)])) {
            XCTAssertEqual($0 as? CaptureQueueError, .duplicateItem)
        }
        // The same identities carrying different bytes conflict.
        let other = Data("other-body".utf8)
        XCTAssertThrowsError(
            try queue.enqueueBatch([
                (try intent(item: "item-1", payload: other), other),
                (two, payload),
            ])
        ) { XCTAssertEqual($0 as? CaptureQueueError, .duplicateItem) }
        XCTAssertEqual(names(root, in: "published").count, 1)
        XCTAssertEqual(queue.allEntries().count, 2)
        XCTAssertTrue(names(root, in: "candidates").isEmpty)
    }

    func testQuotaCountsPublishedAndCandidateBytes() throws {
        let root = try temporaryQueue()
        let payload = Data("pdf-body".utf8)
        let queue = try CaptureIntentQueue(root: root)
        _ = try queue.enqueue(intent: intent(item: "item-1", payload: payload), payload: payload)
        let abandoned = try queue.prepareCandidate([
            (try intent(item: "item-2", payload: payload), payload),
        ])
        let candidate = try queue.prepareCandidate([
            (try intent(item: "item-3", payload: payload), payload),
        ])
        // One byte short of the published payload plus everything prepared.
        queue.ownedBytesLimit = UInt64(payload.count)
            + bytesOnDisk(root.appendingPathComponent("candidates", isDirectory: true))
            - 1

        XCTAssertThrowsError(try queue.publish(candidate)) {
            XCTAssertEqual($0 as? CaptureQueueError, .capacityExceeded)
        }

        // Reclaiming the abandoned candidate's bytes is what makes room.
        queue.discard(abandoned)
        XCTAssertEqual(try queue.publish(candidate).map { $0.intent.itemId }, ["item-3"])
    }

    func testCandidatePreparationNeverRunsUnderThePublicationGate() throws {
        let root = try temporaryQueue()
        let gate = PublicationGate()
        let queue = try CaptureIntentQueue(root: root, gate: gate)
        let payload = Data("pdf-body".utf8)
        let frozen = try intent(payload: payload)
        let box = CandidateBox()
        let reserved = DispatchSemaphore(value: 0)
        let allowWrites = DispatchSemaphore(value: 0)
        let prepared = DispatchSemaphore(value: 0)
        let published = DispatchSemaphore(value: 0)

        queue.afterCandidateReservation = {
            reserved.signal()
            allowWrites.wait()
        }
        Thread.detachNewThread {
            box.candidate = try? queue.prepareCandidate([(frozen, payload)])
            prepared.signal()
        }
        XCTAssertEqual(reserved.wait(timeout: .now() + 5), .success)
        gate.lockForTesting()
        allowWrites.signal()
        XCTAssertEqual(
            prepared.wait(timeout: .now() + 5),
            .success,
            "payload writing after reservation must not need the gate"
        )
        let candidate = try XCTUnwrap(box.candidate)
        XCTAssertEqual(names(root, in: "candidates").count, 1)
        XCTAssertTrue(names(root, in: "published").isEmpty)
        Thread.detachNewThread {
            _ = try? queue.publish(candidate)
            published.signal()
        }
        XCTAssertEqual(
            published.wait(timeout: .now() + 0.5),
            .timedOut,
            "publication must take the gate"
        )
        gate.unlockForTesting()

        XCTAssertEqual(published.wait(timeout: .now() + 5), .success)
        XCTAssertEqual(names(root, in: "published").count, 1)
        XCTAssertEqual(queue.allEntries().count, 1)
    }

    func testLegacyPayloadDirectoryStaysReadableAndIsNotDeliveredTwice() throws {
        let root = try temporaryQueue()
        let payload = Data("pdf-body".utf8)
        let frozen = try intent(payload: payload)
        let payloads = root.appendingPathComponent("payloads", isDirectory: true)
        try FileManager.default.createDirectory(at: payloads, withIntermediateDirectories: true)
        let legacyPayload = payloads.appendingPathComponent("payload-item-1.payload")
        try payload.write(to: legacyPayload)
        let document: [String: Any] = [
            "schema": "omnivia.macos.capture-queue.v1",
            "entries": [
                [
                    "intent": try XCTUnwrap(
                        JSONSerialization.jsonObject(with: frozen.canonicalData()) as? [String: Any]
                    ),
                    "intentDigest": try frozen.requestDigest(),
                    "state": "pending",
                    "attemptGeneration": 0,
                ],
            ],
        ]
        let manifest = root.appendingPathComponent("capture-queue-v1.json")
        try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys]).write(to: manifest)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: manifest.path)

        let queue = try CaptureIntentQueue(root: root)

        XCTAssertEqual(queue.allEntries().map { $0.intent.itemId }, ["item-1"])
        XCTAssertEqual(try queue.payloadURL(for: "item-1").path, legacyPayload.path)
        // Replaying it reconciles against the legacy bytes instead of publishing
        // a second copy of the same item.
        XCTAssertEqual(try queue.enqueue(intent: frozen, payload: payload), queue.allEntries()[0])
        XCTAssertTrue(names(root, in: "published").isEmpty)
        XCTAssertEqual(try Data(contentsOf: queue.payloadURL(for: "item-1")), payload)
    }

    func testDefiniteFailureRetainsTheLastRecoveryPayloadAcrossRestart() throws {
        let root = try temporaryQueue()
        let payload = Data("pdf-body".utf8)
        do {
            let queue = try CaptureIntentQueue(root: root)
            _ = try queue.enqueue(intent: intent(payload: payload), payload: payload)
            _ = try queue.markFailed(itemID: "item-1", failureCode: "delivery-refused")
        }

        let batch = try publishedBatch(root)
        XCTAssertTrue(FileManager.default.fileExists(
            atPath: batch.appendingPathComponent("payload-item-1.payload").path
        ))
        let reopened = try CaptureIntentQueue(root: root)
        XCTAssertEqual(reopened.allEntries().first?.state, .failed)
        XCTAssertTrue(FileManager.default.fileExists(
            atPath: batch.appendingPathComponent("payload-item-1.payload").path
        ))
    }
}
