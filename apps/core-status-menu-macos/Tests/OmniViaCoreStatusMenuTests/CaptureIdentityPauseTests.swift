import CryptoKit
@testable import DocumentIntakeMacOS
import Foundation
import XCTest

/// AT-13 and MON-26: the signed-in principal or the target profile changes while
/// this Mac holds standing work.
///
/// Design §10.2 requires the authenticated principal/context to be confirmed
/// against the original immutable intent before the first possible send and
/// before every recovery attempt, §10.3 requires a changed target/principal
/// identity to pause for the owning setup flow with no silent reassignment, and
/// §21.9.1 makes the same change pause subscriptions and existing pending
/// delivery rather than redirect either. Every test here drives the real queue,
/// the real subscription stores, the real reconciliation schedule and the real
/// monitoring coordinator over one `DocumentIntakeEnvironment`.
final class CaptureIdentityPauseTests: XCTestCase {
    private struct BookmarkCodec: SecurityScopedBookmarkCoding {
        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
        }
        func startAccessing(_ url: URL) -> Bool { true }
        func stopAccessing(_ url: URL) {}
    }

    /// Counts reads of an original, which is how a test says no automatic read
    /// happened at all rather than that one happened and was discarded.
    private final class CountingReader: CoordinatedOriginalReading, @unchecked Sendable {
        private let lock = NSLock()
        private var reads = 0

        var count: Int { lock.withLock { reads } }

        func readStableBytes(from url: URL, maximumBytes: UInt64) throws -> Data {
            lock.withLock { reads += 1 }
            return try SystemCoordinatedOriginalReader()
                .readStableBytes(from: url, maximumBytes: maximumBytes)
        }
    }

    private final class Transport: CaptureDeliveryTransport, @unchecked Sendable {
        let descriptorDigest = "sha256:" + String(repeating: "b", count: 64)
        let deliveryBinding: CaptureDeliveryBinding
        private let outcome: CaptureDeliveryOutcome
        private let lock = NSLock()
        private var sent: [String] = []

        init(_ binding: CaptureDeliveryBinding, outcome: CaptureDeliveryOutcome = .admitted(
            captureID: "dcap-" + String(repeating: "a", count: 64)
        )) {
            deliveryBinding = binding
            self.outcome = outcome
        }

        var sentOperationKeys: [String] { lock.withLock { sent } }

        func deliver(input: Data, payloadURL: URL, operationKey: String) async -> CaptureDeliveryOutcome {
            lock.withLock { sent.append(operationKey) }
            return outcome
        }
    }

    /// `profile-a` and `profile-b` are two installed target profiles, so their
    /// bindings differ in exactly the way `resolve_local_target` derives.
    private static func identity(_ profile: String, workspace: String = "workspace-1") -> CaptureDeliveryBinding {
        .installedCompanion(
            installationState: URL(fileURLWithPath: "/private/tmp/omnivia-\(profile)/state.json"),
            workspaceRef: workspace
        )
    }

    private static func changedPrincipal(of binding: CaptureDeliveryBinding) -> CaptureDeliveryBinding {
        CaptureDeliveryBinding(
            targetRef: binding.targetRef,
            workspaceRef: binding.workspaceRef,
            targetBindingRevision: binding.targetBindingRevision,
            callerContextBinding: CallerContextBinding(
                clientContextRef: binding.callerContextBinding.clientContextRef,
                principalRef: "other-user",
                contextRevision: binding.callerContextBinding.contextRevision
            )
        )
    }

    private struct Lane {
        let environment: DocumentIntakeEnvironment
        let stateRoot: URL
        let original: URL
        let reader: CountingReader
        let authorised: CaptureDeliveryBinding
    }

    /// One watched original, submitted with Keep updated, so the lane holds both
    /// a frozen unsent intent and the file subscription its consent froze.
    private func lane(
        authorised: CaptureDeliveryBinding = CaptureIdentityPauseTests.identity("profile-a")
    ) async throws -> (Lane, [CaptureQueueEntry]) {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("watched.txt")
        try Data("baseline".utf8).write(to: original)
        let reader = CountingReader()
        let stateRoot = root.appendingPathComponent("state", isDirectory: true)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: stateRoot,
            bookmarkCodec: BookmarkCodec(),
            reader: reader
        )
        let submitted = try await ManualCaptureSubmission(
            environment: environment,
            binding: authorised
        ).submit([ManualCaptureCandidate(url: original, keepUpdated: true)])
        return (
            Lane(
                environment: environment,
                stateRoot: stateRoot,
                original: original,
                reader: reader,
                authorised: authorised
            ),
            submitted
        )
    }

    private func delivery(
        _ lane: Lane,
        authenticatedAs binding: CaptureDeliveryBinding,
        outcome: CaptureDeliveryOutcome = .admitted(captureID: "dcap-" + String(repeating: "a", count: 64))
    ) -> (CaptureDeliveryCoordinator, Transport) {
        let transport = Transport(binding, outcome: outcome)
        return (
            CaptureDeliveryCoordinator(
                queue: lane.environment.queue,
                transport: transport,
                fileSubscriptions: lane.environment.fileSubscriptions,
                folderSubscriptions: lane.environment.folderSubscriptions,
                gate: lane.environment.publicationGate
            ),
            transport
        )
    }

    // MARK: - AT-13

    /// The principal changes before the first send. The frozen intent pauses: it
    /// is not sent, not staged, not rewritten towards the new account, and its
    /// recovery copy stays on disk.
    func testPrincipalChangeBeforeFirstSendPausesTheFrozenIntent() async throws {
        let (lane, submitted) = try await lane()
        let itemID = try XCTUnwrap(submitted.first?.intent.itemId)
        let (coordinator, transport) = delivery(
            lane,
            authenticatedAs: Self.changedPrincipal(of: lane.authorised)
        )

        let pause = try await coordinator.pauseWorkUnderChangedIdentity()
        let outcomes = await coordinator.deliverPending()

        XCTAssertEqual(pause.pausedItemIDs, [itemID])
        XCTAssertTrue(outcomes.isEmpty)
        XCTAssertTrue(transport.sentOperationKeys.isEmpty)
        let entry = try XCTUnwrap(lane.environment.queue.allEntries().first { $0.intent.itemId == itemID })
        XCTAssertEqual(entry.state, .pending)
        XCTAssertEqual(entry.attemptGeneration, 0)
        XCTAssertTrue(entry.attempts.isEmpty)
        XCTAssertNil(entry.stageRequestDigest)
        XCTAssertNoThrow(try lane.environment.queue.payloadURL(for: itemID))
        // Nothing was reassigned to the identity that is now signed in.
        XCTAssertEqual(entry.intent.callerContextBinding, lane.authorised.callerContextBinding)
        XCTAssertEqual(entry.intent.targetRef, lane.authorised.targetRef)
        XCTAssertEqual(entry.intent.workspaceRef, lane.authorised.workspaceRef)
    }

    /// The target profile changes, which is a different installed target under
    /// the same principal. Same result: paused, never redirected.
    func testTargetProfileChangeBeforeFirstSendPausesTheFrozenIntent() async throws {
        let (lane, submitted) = try await lane()
        let itemID = try XCTUnwrap(submitted.first?.intent.itemId)
        let other = Self.identity("profile-b")
        XCTAssertNotEqual(other.targetRef, lane.authorised.targetRef)
        let (coordinator, transport) = delivery(lane, authenticatedAs: other)

        let pause = try await coordinator.pauseWorkUnderChangedIdentity()
        _ = await coordinator.deliverPending()

        XCTAssertEqual(pause.pausedItemIDs, [itemID])
        XCTAssertTrue(transport.sentOperationKeys.isEmpty)
        let entry = try XCTUnwrap(lane.environment.queue.allEntries().first { $0.intent.itemId == itemID })
        XCTAssertEqual(entry.state, .pending)
        XCTAssertEqual(entry.intent.targetRef, lane.authorised.targetRef)
    }

    /// A workspace change pauses too, and the authorised identity still
    /// delivers: the pause is the identity comparison, not a blanket refusal.
    func testWorkspaceChangePausesAndTheAuthorisedIdentityStillDelivers() async throws {
        let (lane, submitted) = try await lane()
        let itemID = try XCTUnwrap(submitted.first?.intent.itemId)
        let (changed, changedTransport) = delivery(
            lane,
            authenticatedAs: Self.identity("profile-a", workspace: "workspace-2")
        )

        let changedPause = try await changed.pauseWorkUnderChangedIdentity()
        let changedOutcomes = await changed.deliverPending()
        XCTAssertEqual(changedPause.pausedItemIDs, [itemID])
        XCTAssertTrue(changedOutcomes.isEmpty)
        XCTAssertTrue(changedTransport.sentOperationKeys.isEmpty)

        let (restored, restoredTransport) = delivery(lane, authenticatedAs: lane.authorised)
        let pause = try await restored.pauseWorkUnderChangedIdentity()
        let outcomes = await restored.deliverPending()

        XCTAssertTrue(pause.isEmpty)
        XCTAssertEqual(outcomes.map(\.state), [.admitted])
        XCTAssertEqual(restoredTransport.sentOperationKeys.count, 1)
    }

    /// The recovery variant. An attempt whose outcome is unknown is a recovery
    /// obligation, and §10.2 requires the identity check before every recovery
    /// attempt: the item pauses with its one attempt intact, so nothing is
    /// replayed under the new account, and the no-effect recovery that returns
    /// it to pending does not unpause it either.
    func testIntentAwaitingRecoveryPausesInsteadOfReplayingUnderTheNewAccount() async throws {
        let (lane, submitted) = try await lane()
        let itemID = try XCTUnwrap(submitted.first?.intent.itemId)
        let (authorised, _) = delivery(lane, authenticatedAs: lane.authorised, outcome: .unknown)
        let uncertain = await authorised.deliverPending()
        XCTAssertEqual(uncertain.map(\.state), [.unknownOutcome])
        let attempt = try XCTUnwrap(uncertain.first?.attempts.last)

        let (coordinator, transport) = delivery(
            lane,
            authenticatedAs: Self.changedPrincipal(of: lane.authorised)
        )
        let uncertainPause = try await coordinator.pauseWorkUnderChangedIdentity()
        XCTAssertEqual(uncertainPause.pausedItemIDs, [itemID])
        _ = await coordinator.deliverPending()

        let recovering = try XCTUnwrap(
            lane.environment.queue.allEntries().first { $0.intent.itemId == itemID }
        )
        XCTAssertEqual(recovering.state, .unknownOutcome)
        XCTAssertEqual(recovering.attemptGeneration, 1)
        XCTAssertEqual(recovering.attempts.map(\.operationKey), [attempt.operationKey])
        XCTAssertTrue(transport.sentOperationKeys.isEmpty)

        // The recovery concludes that attempt had no effect, which is the one
        // transition back to pending. Under the changed identity it still pauses
        // and still sends nothing: no replacement key, no send under the new
        // account.
        _ = try lane.environment.queue.recoverKnownNoEffect(
            itemID: itemID,
            attemptGeneration: 1,
            requestDigest: attempt.requestDigest,
            descriptorDigest: attempt.descriptorDigest
        )
        let recoveredPause = try await coordinator.pauseWorkUnderChangedIdentity()
        let recoveredOutcomes = await coordinator.deliverPending()
        XCTAssertEqual(recoveredPause.pausedItemIDs, [itemID])
        XCTAssertTrue(recoveredOutcomes.isEmpty)
        let recovered = try XCTUnwrap(
            lane.environment.queue.allEntries().first { $0.intent.itemId == itemID }
        )
        XCTAssertEqual(recovered.state, .pending)
        XCTAssertEqual(recovered.attemptGeneration, 1)
        XCTAssertEqual(recovered.attempts.count, 1)
        XCTAssertTrue(transport.sentOperationKeys.isEmpty)
    }

    /// A cached Share's adopted intent is frozen like any other, and the Share
    /// inbox itself carries no principal, destination or workspace, so a change
    /// of account finds nothing to inherit and the queued intent pauses.
    func testCachedShareIntentPausesAndCarriesNothingForANewAccountToInherit() async throws {
        let (lane, _) = try await lane()
        let payload = Data("shared".utf8)
        let shared = try Self.shareIntent(payload: payload, binding: lane.authorised)
        try lane.environment.queue.enqueue(intent: shared, payload: payload)
        let (coordinator, transport) = delivery(
            lane,
            authenticatedAs: Self.changedPrincipal(of: lane.authorised)
        )

        let pause = try await coordinator.pauseWorkUnderChangedIdentity()
        _ = await coordinator.deliverPending()

        XCTAssertTrue(pause.pausedItemIDs.contains(shared.itemId))
        XCTAssertTrue(transport.sentOperationKeys.isEmpty)
        let entry = try XCTUnwrap(
            lane.environment.queue.allEntries().first { $0.intent.itemId == shared.itemId }
        )
        XCTAssertEqual(entry.state, .pending)
        XCTAssertEqual(entry.intent.surface, .share)
        XCTAssertEqual(entry.intent.callerContextBinding, lane.authorised.callerContextBinding)
        // The cached request the Share extension wrote holds none of this, so
        // adoption under a new account has nothing to redirect.
        let envelope = ShareInboxEnvelopeV1(
            requestID: "11111111-1111-4111-8111-111111111111",
            senderBundleID: "com.omnivia.core.share",
            createdAt: Date(timeIntervalSince1970: 1_760_000_000),
            items: []
        )
        let encoded = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: try JSONEncoder().encode(envelope))
                as? [String: Any]
        )
        XCTAssertTrue(
            Set(encoded.keys).isDisjoint(with: ["principalRef", "targetRef", "workspaceRef"])
        )
    }

    // MARK: - MON-26

    /// The pending intent is not redirected, and automatic reads pause: both
    /// watches move to `paused` durably, the reconciliation schedule arms
    /// nothing for them afterwards, and the individual-file pass reads no
    /// original at all.
    func testPrincipalChangePausesAutomaticWatchesAndArmsNoFurtherRead() async throws {
        let (lane, submitted) = try await lane()
        let environment = lane.environment
        // Core admits the reviewed baseline, so the file watch is active work
        // rather than a registration still waiting.
        try Self.admit(try XCTUnwrap(submitted.first), queue: environment.queue)
        _ = await environment.fileMonitoring.reconcileAll()
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.active])
        let fileID = try XCTUnwrap(environment.fileSubscriptions.all().first?.subscriptionID)
        try environment.folderSubscriptions.register(
            Self.folderSubscription("folder-1", binding: lane.authorised)
        )
        // The schedule arms both kinds of periodic work while the identity holds.
        XCTAssertEqual(
            Set(try environment.folderReconciliation.schedule().map(\.subscriptionID)),
            ["folder-1"]
        )
        let readsBefore = lane.reader.count

        let (coordinator, transport) = delivery(
            lane,
            authenticatedAs: Self.changedPrincipal(of: lane.authorised)
        )
        let pause = try await coordinator.pauseWorkUnderChangedIdentity()

        XCTAssertEqual(pause.pausedFileSubscriptionIDs, [fileID])
        XCTAssertEqual(pause.pausedFolderSubscriptionIDs, ["folder-1"])
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.paused])
        XCTAssertEqual(environment.folderSubscriptions.all().map(\.state), [.paused])
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.needsReconciliation), [true])
        XCTAssertEqual(environment.folderSubscriptions.all().map(\.needsFullReconciliation), [true])

        // No automatic read is armed or runs: the schedule owes nothing, an
        // immediate trigger arms nothing, and the file pass reads no original.
        XCTAssertTrue(try environment.folderReconciliation.schedule().isEmpty)
        XCTAssertTrue(environment.folderReconciliation.due().isEmpty)
        XCTAssertTrue(
            try environment.folderReconciliation.request(
                subscriptionIDs: ["folder-1"],
                trigger: .wake
            ).isEmpty
        )
        await environment.fileMonitoring.markDirty(subscriptionIDs: [fileID])
        let reports = await environment.fileMonitoring.reconcileAll()
        XCTAssertEqual(reports.map(\.state), [.paused])
        XCTAssertEqual(reports.map(\.queued), [false])
        XCTAssertEqual(lane.reader.count, readsBefore)
        XCTAssertTrue(transport.sentOperationKeys.isEmpty)
        // The watches kept the identity they froze; nothing was rebound.
        XCTAssertEqual(
            environment.fileSubscriptions.all().map { CaptureDeliveryBinding($0) },
            [lane.authorised]
        )
        XCTAssertEqual(
            environment.folderSubscriptions.all().map { CaptureDeliveryBinding($0) },
            [lane.authorised]
        )
    }

    /// A registration still waiting for Core pauses as well: it can reach active
    /// work without a new reviewed consent, and that acknowledgement can never
    /// arrive for the identity it froze.
    func testWatchWaitingForCoreOrNeedingAccessPausesUnderChangedIdentity() async throws {
        let (lane, _) = try await lane()
        let environment = lane.environment
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.waitingForCore])
        try environment.folderSubscriptions.register(
            Self.folderSubscription("folder-1", binding: lane.authorised, state: .needsAccess)
        )
        let (coordinator, _) = delivery(
            lane,
            authenticatedAs: Self.changedPrincipal(of: lane.authorised)
        )

        _ = try await coordinator.pauseWorkUnderChangedIdentity()

        XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.paused])
        XCTAssertEqual(environment.folderSubscriptions.all().map(\.state), [.paused])
    }

    /// The pass changes nothing it has no business changing: a watch frozen
    /// against the authenticated identity stays active, and a stopped watch and
    /// one already waiting for its owner's review are left exactly as they are.
    func testPassLeavesMatchingStoppedAndReviewRequiredWatchesAlone() async throws {
        let (lane, submitted) = try await lane()
        let environment = lane.environment
        try Self.admit(try XCTUnwrap(submitted.first), queue: environment.queue)
        _ = await environment.fileMonitoring.reconcileAll()
        try environment.folderSubscriptions.register(
            Self.folderSubscription("stopped", binding: Self.identity("profile-b"), state: .stopped)
        )
        try environment.folderSubscriptions.register(
            Self.folderSubscription(
                "review",
                binding: Self.identity("profile-b"),
                state: .reviewRequired
            )
        )
        try environment.folderSubscriptions.register(
            Self.folderSubscription("matching", binding: lane.authorised)
        )
        let (coordinator, _) = delivery(lane, authenticatedAs: lane.authorised)

        let pause = try await coordinator.pauseWorkUnderChangedIdentity()

        XCTAssertTrue(pause.isEmpty)
        XCTAssertEqual(
            environment.folderSubscriptions.all().map { ($0.subscriptionID, $0.state) }.map(\.1),
            [.active, .reviewRequired, .stopped]
        )
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.active])
    }

    /// A pause that cannot be made durable is not reported as done. The watch is
    /// left exactly as it was — which is why the automatic passes abandon their
    /// run on a throw rather than read under the new identity — and the send
    /// boundary still refuses the frozen intent on its own.
    func testPauseThatCannotBePersistedThrowsAndLeavesTheWatchAsItWas() async throws {
        let (lane, submitted) = try await lane()
        let environment = lane.environment
        // The owner's own state directory, which `DocumentIntakeEnvironment`
        // lays out under the state root. Made unwritable so the durable write
        // the pause owes genuinely fails.
        let directory = lane.stateRoot.appendingPathComponent("file-subscriptions", isDirectory: true)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o500],
            ofItemAtPath: directory.path
        )
        addTeardownBlock {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o700],
                ofItemAtPath: directory.path
            )
        }
        let (coordinator, transport) = delivery(
            lane,
            authenticatedAs: Self.changedPrincipal(of: lane.authorised)
        )

        var thrown: Error?
        do {
            _ = try await coordinator.pauseWorkUnderChangedIdentity()
        } catch {
            thrown = error
        }

        XCTAssertEqual(thrown as? FileSubscriptionStoreError, .stateUnavailable)
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.waitingForCore])
        let outcomes = await coordinator.deliverPending()
        XCTAssertTrue(outcomes.isEmpty)
        XCTAssertTrue(transport.sentOperationKeys.isEmpty)
        let entry = try XCTUnwrap(
            environment.queue.allEntries().first { $0.intent.itemId == submitted[0].intent.itemId }
        )
        XCTAssertEqual(entry.state, .pending)
    }

    // MARK: - Fixtures

    private static func admit(_ entry: CaptureQueueEntry, queue: CaptureIntentQueue) throws {
        let digest = "sha256:" + String(repeating: "d", count: 64)
        let staging = try queue.beginStage(
            itemID: entry.intent.itemId,
            requestDigest: digest,
            descriptorDigest: digest
        )
        _ = try queue.markAdmitted(
            itemID: entry.intent.itemId,
            attemptGeneration: staging.attemptGeneration,
            captureID: "dcap-" + String(repeating: "a", count: 64)
        )
    }

    private static func shareIntent(
        payload: Data,
        binding: CaptureDeliveryBinding
    ) throws -> CaptureIntentV3 {
        try CaptureIntentV3(
            submissionId: "submission-share",
            itemId: "item-share",
            targetRef: binding.targetRef,
            workspaceRef: binding.workspaceRef,
            targetBindingRevision: binding.targetBindingRevision,
            callerContextBinding: binding.callerContextBinding,
            surface: .share,
            contentKind: .text,
            payloadRef: "payload-share",
            byteLength: UInt64(payload.count),
            localIntegrityDigest: "sha256:" + SHA256.hash(data: payload)
                .map { String(format: "%02x", $0) }.joined(),
            requestedSourceIdentity: "source-share",
            operationKey: "capture-share",
            intentKind: .newCapture,
            basePrecondition: .create,
            processingProfile: ProcessingProfileBinding(profileRef: "document-default", revision: "1"),
            createdAt: "2026-10-16T00:00:00Z",
            displayName: "shared.txt"
        )
    }

    private static func folderSubscription(
        _ id: String,
        binding: CaptureDeliveryBinding,
        state: FolderSubscriptionState = .active
    ) -> FolderSubscription {
        FolderSubscription(
            subscriptionID: id,
            rootRef: "root-\(id)",
            rootDisplayName: "Root",
            targetRef: binding.targetRef,
            workspaceRef: binding.workspaceRef,
            targetBindingRevision: binding.targetBindingRevision,
            callerContextBinding: binding.callerContextBinding,
            recursive: true,
            includeHidden: false,
            configGeneration: 1,
            consentGeneration: 1,
            consentRecordedAt: "2026-10-16T00:00:00Z",
            observationEpoch: "epoch-1",
            state: state,
            needsFullReconciliation: false,
            lastCompletedMetadataScanAt: nil,
            pendingReviewSignature: nil,
            approvedReviewSignature: nil,
            reviewChangeCount: nil,
            reviewExamples: nil,
            members: []
        )
    }

    private func temporaryDirectory() throws -> URL {
        let url = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("omnivia-identity-pause-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
