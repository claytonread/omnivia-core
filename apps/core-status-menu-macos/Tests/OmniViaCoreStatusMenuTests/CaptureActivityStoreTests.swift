import CryptoKit
import Foundation
import XCTest
@testable import DocumentIntakeMacOS

final class CaptureActivityStoreTests: XCTestCase {
    private let binding = CallerContextBinding(
        clientContextRef: "client-1",
        principalRef: "principal-1",
        contextRevision: "context-1"
    )
    private let stageDigest = "sha256:" + String(repeating: "a", count: 64)
    private let descriptorDigest = "sha256:" + String(repeating: "b", count: 64)
    private let captureID = "dcap-" + String(repeating: "c", count: 64)
    private let now = ISO8601DateFormatter().date(from: "2026-10-15T12:00:00Z")!

    // MARK: - Fixtures

    private func temporaryDirectory(_ name: String) throws -> URL {
        let url = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("omnivia-\(name)-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    private func store(at directory: URL) throws -> CaptureActivityStore {
        try CaptureActivityStore(stateDirectory: directory)
    }

    private func digest(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func intent(
        item: String,
        submission: String = "submission-1",
        payload: Data,
        name: String = "Document.pdf"
    ) throws -> CaptureIntentV3 {
        try CaptureIntentV3(
            submissionId: submission,
            itemId: item,
            targetRef: "target-1",
            workspaceRef: "ws-1",
            targetBindingRevision: "target-revision-1",
            callerContextBinding: binding,
            surface: .manual,
            contentKind: .pdf,
            payloadRef: "payload-" + item,
            byteLength: UInt64(payload.count),
            localIntegrityDigest: digest(payload),
            requestedSourceIdentity: "source-" + item,
            operationKey: "operation-" + item,
            intentKind: .newCapture,
            basePrecondition: .create,
            processingProfile: ProcessingProfileBinding(
                profileRef: "profile-1",
                revision: "profile-revision-1"
            ),
            createdAt: "2026-10-14T00:00:00Z",
            displayName: name
        )
    }

    private func member(
        index: Int,
        run: String = "run-1",
        state: FolderBulkMemberState = .planned,
        failureCode: String? = nil
    ) -> FolderBulkRunMember {
        FolderBulkRunMember(
            memberID: "member-\(run)-\(index)",
            accessRef: "access-\(run)-\(index)",
            linkedDocumentID: "document-\(run)-\(index)",
            requestedSourceIdentity: "identity-\(run)-\(index)",
            relativeComponents: ["file-\(index).pdf"],
            displayName: "file-\(index).pdf",
            contentKind: .pdf,
            bindingGeneration: 1,
            itemID: "item-\(run)-\(index)",
            payloadRef: "payload-\(run)-\(index)",
            operationKey: "operation-\(run)-\(index)",
            declaredByteLength: 16,
            state: state,
            failureCode: failureCode
        )
    }

    private func run(
        id: String = "run-1",
        state: FolderBulkRunState = .adding,
        members: [FolderBulkRunMember]
    ) -> FolderBulkRun {
        FolderBulkRun(
            runID: id,
            manifestRevision: "manifest-1",
            submissionID: "submission-\(id)",
            rootRef: "root-\(id)",
            rootDisplayName: "Invoices",
            subscriptionID: nil,
            targetRef: "target-1",
            workspaceRef: "ws-1",
            targetBindingRevision: "target-revision-1",
            callerContextBinding: binding,
            createdAt: "2026-10-15T07:00:00Z",
            state: state,
            members: members
        )
    }

    private func date(_ value: String) throws -> Date {
        try XCTUnwrap(ISO8601DateFormatter().date(from: value))
    }

    // MARK: - Facts

    func testEveryQueueStateBecomesItsOwnSeparateFact() throws {
        let root = try temporaryDirectory("queue")
        let queue = try CaptureIntentQueue(root: root, gate: PublicationGate())
        let subject = try store(at: try temporaryDirectory("activity"))
        for index in 0..<5 {
            let payload = Data("payload-\(index)".utf8)
            try queue.enqueue(intent: try intent(item: "item-\(index)", payload: payload), payload: payload)
        }
        // Everything is recorded while it is still pending, which is what makes
        // each later outcome observable at all.
        _ = try subject.observe(queue: queue.allEntries(), runs: [], now: now)

        let staging = try queue.beginStage(
            itemID: "item-1",
            requestDigest: stageDigest,
            descriptorDigest: descriptorDigest
        )
        _ = try queue.beginStage(
            itemID: "item-2",
            requestDigest: stageDigest,
            descriptorDigest: descriptorDigest
        )
        _ = try queue.markUnknownOutcome(itemID: "item-2", attemptGeneration: 1)
        _ = try queue.beginStage(
            itemID: "item-3",
            requestDigest: stageDigest,
            descriptorDigest: descriptorDigest
        )
        _ = try queue.markAdmitted(itemID: "item-3", attemptGeneration: 1, captureID: captureID)
        _ = try queue.markFailed(itemID: "item-4", failureCode: "download_failed")

        let records = try subject.observe(queue: queue.allEntries(), runs: [], now: now)
        let facts = Dictionary(uniqueKeysWithValues: records.map { ($0.itemID, $0.fact) })

        XCTAssertEqual(facts["item-0"], .queued)
        XCTAssertEqual(facts["item-1"], .sending)
        XCTAssertEqual(facts["item-2"], .checking)
        XCTAssertEqual(facts["item-3"], .processing)
        XCTAssertEqual(facts["item-4"], .failed)
        XCTAssertEqual(staging.attemptGeneration, 1)
        XCTAssertEqual(
            try XCTUnwrap(subject.record(itemID: "item-3")).admittedCaptureID,
            captureID,
            "an accepted capture id is a fact; it is still not a retrieval-ready receipt"
        )
        XCTAssertEqual(
            try XCTUnwrap(subject.record(itemID: "item-4")).failureCode,
            "download_failed"
        )
    }

    func testFolderMembersCarryTheirRunAndTheStatesThatExistBeforeTheQueue() throws {
        let subject = try store(at: try temporaryDirectory("activity"))
        var frozen = run(members: (0..<3).map { member(index: $0) })
        _ = try subject.observe(queue: [], runs: [frozen], now: now)
        frozen.members[1].state = .stopped
        frozen.members[2].state = .needsAttention
        frozen.members[2].failureCode = "preparation_failed"

        let records = try subject.observe(queue: [], runs: [frozen], now: now)

        XCTAssertEqual(records.map(\.fact), [.planned, .stopped, .failed])
        XCTAssertEqual(records.map(\.runID), ["run-1", "run-1", "run-1"])
        XCTAssertEqual(records.map(\.memberID), [
            "member-run-1-0", "member-run-1-1", "member-run-1-2",
        ])
        XCTAssertEqual(records.map(\.displayName), ["file-0.pdf", "file-1.pdf", "file-2.pdf"])
        XCTAssertEqual(records.map(\.submissionID), Array(repeating: "submission-run-1", count: 3))
        XCTAssertEqual(records[2].failureCode, "preparation_failed")
    }

    func testTheQueueDecidesDeliveryFactsForAMemberItHolds() throws {
        let root = try temporaryDirectory("queue")
        let queue = try CaptureIntentQueue(root: root, gate: PublicationGate())
        let subject = try store(at: try temporaryDirectory("activity"))
        let payload = Data("member".utf8)
        try queue.enqueue(
            intent: try intent(item: "item-run-1-0", payload: payload, name: "file-0.pdf"),
            payload: payload
        )
        // The run ledger still calls it published, which collapses queued,
        // sending and unknown into one word.
        let frozen = run(members: [member(index: 0, state: .published)])
        _ = try subject.observe(queue: queue.allEntries(), runs: [frozen], now: now)
        _ = try queue.beginStage(
            itemID: "item-run-1-0",
            requestDigest: stageDigest,
            descriptorDigest: descriptorDigest
        )

        let records = try subject.observe(queue: queue.allEntries(), runs: [frozen], now: now)

        XCTAssertEqual(records.map(\.fact), [.sending])
        XCTAssertEqual(records.first?.runID, "run-1", "it is still a member of its run")
        XCTAssertEqual(records.first?.memberID, "member-run-1-0")
    }

    func testAMixedAdditionKeepsEveryItemsOwnOutcome() throws {
        let root = try temporaryDirectory("queue")
        let queue = try CaptureIntentQueue(root: root, gate: PublicationGate())
        let subject = try store(at: try temporaryDirectory("activity"))
        for index in 0..<2 {
            let payload = Data("payload-\(index)".utf8)
            try queue.enqueue(
                intent: try intent(item: "item-run-1-\(index)", payload: payload, name: "file-\(index).pdf"),
                payload: payload
            )
        }
        var frozen = run(members: [
            member(index: 0, state: .published),
            member(index: 1, state: .published),
            member(index: 2, state: .planned),
            member(index: 3, state: .planned),
        ])
        _ = try subject.observe(queue: queue.allEntries(), runs: [frozen], now: now)
        frozen.members[3].state = .stopped
        _ = try queue.beginStage(
            itemID: "item-run-1-0",
            requestDigest: stageDigest,
            descriptorDigest: descriptorDigest
        )
        _ = try queue.markAdmitted(
            itemID: "item-run-1-0",
            attemptGeneration: 1,
            captureID: captureID
        )
        _ = try queue.markFailed(itemID: "item-run-1-1", failureCode: "integrity_mismatch")
        frozen.state = .needsAttention

        let records = try subject.observe(queue: queue.allEntries(), runs: [frozen], now: now)

        XCTAssertEqual(records.map(\.fact), [.processing, .failed, .planned, .stopped])
        XCTAssertEqual(records.filter { $0.fact.isResolved }.count, 3)
        XCTAssertEqual(
            records.filter(\.isReceipt).map(\.itemID),
            ["item-run-1-0", "item-run-1-3"],
            "a failed member its run can retry is unresolved work, not a receipt"
        )
        XCTAssertEqual(records.filter { $0.fact.needsAttention }.map(\.itemID), ["item-run-1-1"])
    }

    // MARK: - Durability

    func testActivitySurvivesARestartWithItsFactsAndIdentitiesIntact() throws {
        let directory = try temporaryDirectory("activity")
        let root = try temporaryDirectory("queue")
        let queue = try CaptureIntentQueue(root: root, gate: PublicationGate())
        let payload = Data("payload".utf8)
        try queue.enqueue(intent: try intent(item: "item-1", payload: payload), payload: payload)
        let frozen = run(members: [member(index: 0, state: .planned)])
        do {
            let subject = try store(at: directory)
            _ = try subject.observe(queue: queue.allEntries(), runs: [frozen], now: now)
            _ = try queue.beginStage(
                itemID: "item-1",
                requestDigest: stageDigest,
                descriptorDigest: descriptorDigest
            )
            _ = try queue.markUnknownOutcome(itemID: "item-1", attemptGeneration: 1)
            _ = try subject.observe(queue: queue.allEntries(), runs: [frozen], now: now)
        }

        // A new process, reading nothing but the file the last one wrote.
        let reopened = try store(at: directory)
        let records = reopened.all()

        XCTAssertEqual(records.map(\.itemID), ["item-run-1-0", "item-1"])
        XCTAssertEqual(records.map(\.fact), [.planned, .checking])
        let unknown = try XCTUnwrap(reopened.record(itemID: "item-1"))
        XCTAssertEqual(unknown.operationKey, "operation-item-1")
        XCTAssertEqual(unknown.attemptGeneration, 1)
        XCTAssertEqual(unknown.attemptRequestDigest, stageDigest)
        XCTAssertEqual(unknown.attemptDescriptorDigest, descriptorDigest)
        XCTAssertEqual(
            unknown.attemptOperationKey,
            CaptureAttemptRecord.childOperationKey(parent: "operation-item-1", generation: 1)
        )
    }

    func testASequenceIsAssignedOnceAndIsNeverRewritten() throws {
        let directory = try temporaryDirectory("activity")
        let frozen = run(members: [member(index: 0, state: .planned)])
        let subject = try store(at: directory)
        let first = try subject.observe(queue: [], runs: [frozen], now: now)
        var advanced = frozen
        advanced.members[0].state = .stopped
        let second = try subject.observe(
            queue: [],
            runs: [advanced],
            now: try date("2026-10-15T13:00:00Z")
        )

        XCTAssertEqual(first.map(\.sequence), [1])
        XCTAssertEqual(second.map(\.sequence), [1])
        XCTAssertEqual(first.first?.firstObservedAt, second.first?.firstObservedAt)
        XCTAssertEqual(second.first?.resolvedAt, "2026-10-15T13:00:00Z")
        XCTAssertNil(first.first?.resolvedAt, "an unstarted member has resolved nothing")
    }

    func testStateIsWrittenOwnerOnly() throws {
        let directory = try temporaryDirectory("activity")
        let subject = try store(at: directory)
        _ = try subject.observe(
            queue: [],
            runs: [run(members: [member(index: 0)])],
            now: now
        )
        let file = directory.appendingPathComponent("capture-activity-v1.json")
        let attributes = try FileManager.default.attributesOfItem(atPath: file.path)

        XCTAssertEqual((attributes[.posixPermissions] as? NSNumber)?.intValue, 0o600)
        XCTAssertEqual(
            (attributes[.ownerAccountID] as? NSNumber)?.uint32Value,
            Darwin.getuid()
        )
    }

    func testAnUnknownSchemaIsRefusedRatherThanReadAsASubset() throws {
        let directory = try temporaryDirectory("activity")
        _ = try store(at: directory)
        let file = directory.appendingPathComponent("capture-activity-v1.json")
        try Data(#"{"schema":"omnivia.macos.capture-activity.v2","nextSequence":1,"records":[]}"#.utf8)
            .write(to: file)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)

        XCTAssertThrowsError(try store(at: directory)) { error in
            XCTAssertEqual(error as? CaptureActivityError, .invalidState)
        }
    }

    // MARK: - Retention

    func testTerminalActivityOlderThanThirtyDaysIsDropped() throws {
        let subject = try store(at: try temporaryDirectory("activity"))
        var frozen = run(members: [member(index: 0), member(index: 1)])
        _ = try subject.observe(queue: [], runs: [frozen], now: now)
        frozen.members[0].state = .stopped
        _ = try subject.observe(queue: [], runs: [frozen], now: now)

        // Thirty days and an hour later. The still-unstarted member stays
        // whatever its age, because nothing about it is resolved.
        let later = now.addingTimeInterval(30 * 24 * 60 * 60 + 3600)
        let records = try subject.observe(queue: [], runs: [frozen], now: later)

        XCTAssertEqual(records.map(\.itemID), ["item-run-1-1"])
        XCTAssertEqual(records.map(\.fact), [.planned])
    }

    func testTerminalActivityIsKeptWhileItIsInsideTheWindow() throws {
        let subject = try store(at: try temporaryDirectory("activity"))
        var frozen = run(members: [member(index: 0)])
        _ = try subject.observe(queue: [], runs: [frozen], now: now)
        frozen.members[0].state = .stopped
        _ = try subject.observe(queue: [], runs: [frozen], now: now)

        let records = try subject.observe(
            queue: [],
            runs: [frozen],
            now: now.addingTimeInterval(29 * 24 * 60 * 60)
        )

        XCTAssertEqual(records.map(\.fact), [.stopped])
        XCTAssertEqual(records.first?.resolvedAt, "2026-10-15T12:00:00Z")
    }

    func testAtMostOneHundredTerminalItemsAreRetainedOldestFirstToGo() throws {
        let subject = try store(at: try temporaryDirectory("activity"))
        var members = (0..<101).map { member(index: $0) }
        _ = try subject.observe(queue: [], runs: [run(members: members)], now: now)
        for index in members.indices { members[index].state = .stopped }
        // One member stays unstarted, so the bound is applied beside live work.
        members.append(member(index: 999))

        let records = try subject.observe(queue: [], runs: [run(members: members)], now: now)
        let receipts = records.filter(\.isReceipt)

        XCTAssertEqual(receipts.count, CaptureActivityStore.maximumRetainedReceipts)
        XCTAssertEqual(
            receipts.first?.itemID,
            "item-run-1-1",
            "the oldest receipt is the one that goes"
        )
        XCTAssertEqual(records.filter { !$0.isReceipt }.map(\.itemID), ["item-run-1-999"])
        XCTAssertEqual(records.count, 101)
    }

    func testPrunedActivityIsNotAdoptedAgainFromTheOwnersThatStillHoldIt() throws {
        let subject = try store(at: try temporaryDirectory("activity"))
        var frozen = run(members: [member(index: 0)])
        _ = try subject.observe(queue: [], runs: [frozen], now: now)
        frozen.members[0].state = .stopped
        _ = try subject.observe(queue: [], runs: [frozen], now: now)
        let later = now.addingTimeInterval(31 * 24 * 60 * 60)
        XCTAssertTrue(try subject.observe(queue: [], runs: [frozen], now: later).isEmpty)

        // The frozen manifest has not changed and still names the member.
        let again = try subject.observe(queue: [], runs: [frozen], now: later)

        XCTAssertTrue(
            again.isEmpty,
            "re-adopting it would resurrect a receipt and invent a resolution time"
        )
    }

    // MARK: - Clearing

    func testClearingRemovesTerminalActivityAndKeepsUnresolvedWork() throws {
        let root = try temporaryDirectory("queue")
        let queue = try CaptureIntentQueue(root: root, gate: PublicationGate())
        let runDirectory = try temporaryDirectory("runs")
        let runStore = try FolderBulkRunStore(stateDirectory: runDirectory)
        let subject = try store(at: try temporaryDirectory("activity"))
        for index in 0..<3 {
            let payload = Data("payload-\(index)".utf8)
            try queue.enqueue(intent: try intent(item: "item-\(index)", payload: payload), payload: payload)
        }
        try runStore.register(
            run(members: [member(index: 0), member(index: 1), member(index: 2)])
        )
        _ = try subject.observe(queue: queue.allEntries(), runs: runStore.all(), now: now)
        // Stop adding remaining files, and separately leave one member that the
        // run can still retry.
        try runStore.update(id: "run-1") { value in
            value.members[1].state = .stopped
            value.members[2].state = .needsAttention
            value.members[2].failureCode = "unavailable"
        }
        _ = try queue.beginStage(
            itemID: "item-0",
            requestDigest: stageDigest,
            descriptorDigest: descriptorDigest
        )
        _ = try queue.markAdmitted(itemID: "item-0", attemptGeneration: 1, captureID: captureID)
        _ = try queue.markFailed(itemID: "item-1", failureCode: "download_failed")
        _ = try queue.beginStage(
            itemID: "item-2",
            requestDigest: stageDigest,
            descriptorDigest: descriptorDigest
        )
        _ = try queue.markUnknownOutcome(itemID: "item-2", attemptGeneration: 1)
        _ = try subject.observe(queue: queue.allEntries(), runs: runStore.all(), now: now)
        let queueBefore = queue.allEntries()
        let runsBefore = runStore.all()
        XCTAssertEqual(subject.receiptCount(), 3)

        let removed = try subject.clearReceipts()
        let remaining = try subject.observe(queue: queue.allEntries(), runs: runStore.all(), now: now)

        XCTAssertEqual(
            removed,
            3,
            "the accepted item, the failed ordinary item and the stopped member"
        )
        XCTAssertEqual(
            remaining.map(\.itemID),
            ["item-run-1-0", "item-run-1-2", "item-2"]
        )
        XCTAssertEqual(
            remaining.map(\.fact),
            [.planned, .failed, .checking],
            "an unstarted member, a member its run can retry and an unknown outcome all stay"
        )
        // Nothing else moved: the queue keeps every entry, key and attempt, and
        // the run ledger keeps its frozen membership.
        XCTAssertEqual(queue.allEntries(), queueBefore)
        XCTAssertEqual(runStore.all(), runsBefore)
        XCTAssertEqual(
            try XCTUnwrap(queue.allEntries().first { $0.intent.itemId == "item-1" }).state,
            .failed,
            "the failed item's recovery copy and key are still the queue's to keep"
        )
        XCTAssertNoThrow(try queue.payloadURL(for: "item-2"))
    }

    func testClearedActivityStaysClearedWhileTheOwnersStillHoldTheItem() throws {
        let root = try temporaryDirectory("queue")
        let queue = try CaptureIntentQueue(root: root, gate: PublicationGate())
        let subject = try store(at: try temporaryDirectory("activity"))
        let payload = Data("payload".utf8)
        try queue.enqueue(intent: try intent(item: "item-1", payload: payload), payload: payload)
        _ = try subject.observe(queue: queue.allEntries(), runs: [], now: now)
        _ = try queue.markFailed(itemID: "item-1", failureCode: "download_failed")
        _ = try subject.observe(queue: queue.allEntries(), runs: [], now: now)
        _ = try subject.clearReceipts()

        let again = try subject.observe(queue: queue.allEntries(), runs: [], now: now)

        XCTAssertTrue(again.isEmpty)
        XCTAssertEqual(
            queue.allEntries().first?.state,
            .failed,
            "clearing a receipt is not a removal from the queue"
        )
    }

    func testClearingNothingTerminalChangesNothing() throws {
        let directory = try temporaryDirectory("activity")
        let subject = try store(at: directory)
        _ = try subject.observe(queue: [], runs: [run(members: [member(index: 0)])], now: now)
        let before = subject.all()

        XCTAssertEqual(try subject.clearReceipts(), 0)
        XCTAssertEqual(subject.all(), before)
    }

    func testClearingKeepsAFailedMemberItsRunCanStillRetry() throws {
        let subject = try store(at: try temporaryDirectory("activity"))
        let frozen = run(members: [member(index: 0, state: .needsAttention, failureCode: "unavailable")])
        _ = try subject.observe(queue: [], runs: [frozen], now: now)

        XCTAssertEqual(subject.receiptCount(), 0)
        XCTAssertEqual(try subject.clearReceipts(), 0)
        XCTAssertEqual(
            subject.all().map(\.fact),
            [.failed],
            "this record is the handle Retry unavailable files is chosen from"
        )
    }

    func testARetriedMemberKeepsItsFrozenIdentityAndDropsItsResolvedTime() throws {
        let subject = try store(at: try temporaryDirectory("activity"))
        var frozen = run(members: [member(index: 0, state: .needsAttention, failureCode: "unavailable")])
        _ = try subject.observe(queue: [], runs: [frozen], now: now)
        XCTAssertEqual(subject.record(itemID: "item-run-1-0")?.resolvedAt, "2026-10-15T12:00:00Z")

        // Retry unavailable files returns the same member to the same plan.
        frozen.members[0].state = .planned
        frozen.members[0].failureCode = nil
        _ = try subject.observe(queue: [], runs: [frozen], now: try date("2026-10-16T09:00:00Z"))

        let record = try XCTUnwrap(subject.record(itemID: "item-run-1-0"))
        XCTAssertEqual(record.fact, .planned)
        XCTAssertNil(record.resolvedAt)
        XCTAssertNil(record.failureCode)
        XCTAssertEqual(record.itemID, "item-run-1-0")
        XCTAssertEqual(record.operationKey, "operation-run-1-0")
        XCTAssertEqual(record.memberID, "member-run-1-0")
        XCTAssertEqual(record.sequence, 1, "it is the same work, not a new item")
    }

    func testAClearedMemberRecordedAgainTakesANewSequence() throws {
        let subject = try store(at: try temporaryDirectory("activity"))
        var frozen = run(members: [member(index: 0)])
        _ = try subject.observe(queue: [], runs: [frozen], now: now)
        frozen.members[0].state = .stopped
        _ = try subject.observe(queue: [], runs: [frozen], now: now)
        XCTAssertEqual(try subject.clearReceipts(), 1)

        // A stopped member that is planned again is new local work.
        frozen.members[0].state = .planned
        let records = try subject.observe(
            queue: [],
            runs: [frozen],
            now: try date("2026-10-16T09:00:00Z")
        )

        XCTAssertEqual(records.map(\.itemID), ["item-run-1-0"])
        XCTAssertEqual(
            records.first?.sequence,
            2,
            "numbering is not rewound, so a cleared receipt is never confused with this one"
        )
        XCTAssertEqual(records.first?.firstObservedAt, "2026-10-16T09:00:00Z")
    }

    // MARK: - Composition

    func testTheComposedEnvironmentRecordsRealActivityAndKeepsItAcrossARestart() throws {
        let stateRoot = try temporaryDirectory("state")
        let payload = Data("payload".utf8)
        do {
            let environment = try DocumentIntakeEnvironment(stateRoot: stateRoot)
            try environment.queue.enqueue(
                intent: try intent(item: "item-1", payload: payload, name: "Invoice.pdf"),
                payload: payload
            )
            let recorded = environment.recordCaptureActivity()
            XCTAssertEqual(recorded.map(\.itemID), ["item-1"])
            XCTAssertEqual(recorded.map(\.fact), [.queued])
            _ = try environment.queue.beginStage(
                itemID: "item-1",
                requestDigest: stageDigest,
                descriptorDigest: descriptorDigest
            )
            _ = try environment.queue.markAdmitted(
                itemID: "item-1",
                attemptGeneration: 1,
                captureID: captureID
            )
            XCTAssertEqual(environment.recordCaptureActivity().map(\.fact), [.processing])
        }

        // A new process over the same state root reads the activity from disk,
        // and recording again neither duplicates it nor rewrites its identity.
        let reopened = try DocumentIntakeEnvironment(stateRoot: stateRoot)
        XCTAssertEqual(reopened.captureActivity.all().map(\.itemID), ["item-1"])
        XCTAssertEqual(reopened.captureActivity.all().map(\.fact), [.processing])

        let again = reopened.recordCaptureActivity()
        XCTAssertEqual(again.map(\.itemID), ["item-1"])
        XCTAssertEqual(again.first?.sequence, 1)
        XCTAssertEqual(again.first?.admittedCaptureID, captureID)
    }

    // MARK: - Adoption

    func testATerminalItemIsNotAdoptedWhenThisStoreNeverSawItUnresolved() throws {
        let root = try temporaryDirectory("queue")
        let queue = try CaptureIntentQueue(root: root, gate: PublicationGate())
        let subject = try store(at: try temporaryDirectory("activity"))
        let payload = Data("payload".utf8)
        try queue.enqueue(intent: try intent(item: "item-1", payload: payload), payload: payload)
        _ = try queue.markFailed(itemID: "item-1", failureCode: "download_failed")

        XCTAssertTrue(try subject.observe(queue: queue.allEntries(), runs: [], now: now).isEmpty)
    }

    func testAnUnresolvedItemIsAdoptedAfterARestartThatNeverSawItBefore() throws {
        let root = try temporaryDirectory("queue")
        let queue = try CaptureIntentQueue(root: root, gate: PublicationGate())
        let payload = Data("payload".utf8)
        try queue.enqueue(intent: try intent(item: "item-1", payload: payload), payload: payload)
        _ = try queue.beginStage(
            itemID: "item-1",
            requestDigest: stageDigest,
            descriptorDigest: descriptorDigest
        )
        _ = try queue.markUnknownOutcome(itemID: "item-1", attemptGeneration: 1)
        let subject = try store(at: try temporaryDirectory("activity"))

        let records = try subject.observe(queue: queue.allEntries(), runs: [], now: now)

        XCTAssertEqual(records.map(\.fact), [.checking])
        XCTAssertEqual(
            records.first?.attemptGeneration,
            1,
            "the frozen attempt is what it is reconciled under; no replacement is allocated"
        )
    }
}
