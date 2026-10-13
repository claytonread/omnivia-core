import CryptoKit
import Foundation
import XCTest
@testable import DocumentIntakeMacOS

final class CaptureIntentQueueTests: XCTestCase {
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
}
