import Foundation
import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

/// AT-30 · source-level Disconnect (design §13.3) when Core's acknowledgement is lost.
///
/// Every test here drives the production path: `DocumentIntakeEnvironment.disconnectSource`
/// (what the window's Disconnect control calls), the durable `NativeAccessStore` intent, and
/// `OriginalDisconnectReconciler`. The only stand-in is Core itself. `CommittingCore`
/// models exactly one property of Core's `document.original.disconnect` operation: a key it
/// has committed answers a second time with the same result and does not act again. The
/// live arm in `OriginalDisconnectLiveCoreTests` removes the stand-in and runs the real CLI
/// against a real service.
final class OriginalDisconnectAcceptanceTests: XCTestCase {
    private struct BookmarkCodec: SecurityScopedBookmarkCoding {
        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
        }
        func startAccessing(_ url: URL) -> Bool { true }
        func stopAccessing(_ url: URL) {}
    }

    private final class TestClock: @unchecked Sendable {
        private let lock = NSLock()
        private var value: Int64 = 0
        func advance(_ delta: Int64) { lock.withLock { value += delta } }
        var reader: @Sendable () -> Int64 { { [self] in self.lock.withLock { self.value } } }
    }

    /// Core's side of an idempotent mutation, and nothing else: the first request under a
    /// key acts, any later request under it answers the stored result, and a different body
    /// under a used key is refused. `loseAcknowledgements` drops the answer after the commit.
    private final class CommittingCore: OriginalDisconnectTransport, @unchecked Sendable {
        let deliveryBinding: CaptureDeliveryBinding
        private let lock = NSLock()
        private var stored: [String: Data] = [:]
        private var lost = 0
        private var online = true
        private var requests: [(key: String, input: Data)] = []
        private var acts = 0

        init(binding: CaptureDeliveryBinding = .companion(workspaceRef: "workspace-1")) {
            deliveryBinding = binding
        }

        var received: [(key: String, input: Data)] { lock.withLock { requests } }
        /// How many times Core actually changed a binding.
        var mutations: Int { lock.withLock { acts } }
        func loseNextAcknowledgements(_ count: Int) { lock.withLock { lost = count } }
        func setOnline(_ value: Bool) { lock.withLock { online = value } }

        func deliver(input: Data, operationKey: String) async -> OriginalDisconnectOutcome {
            lock.withLock {
                guard online else { return .notSent }
                requests.append((operationKey, input))
                if let prior = stored[operationKey] {
                    guard prior == input else { return .definiteFailure(code: "idempotency_conflict") }
                } else {
                    stored[operationKey] = input
                    acts += 1
                }
                if lost > 0 {
                    lost -= 1
                    return .unknown
                }
                guard let body = (try? JSONSerialization.jsonObject(with: input)) as? [String: Any],
                      let documentID = body["document_id"] as? String,
                      let ownerRef = body["owner_ref"] as? String
                else { return .definiteFailure(code: "invalid_request") }
                return .acknowledged(documentID: documentID, ownerRef: ownerRef)
            }
        }
    }

    private final class RefusingCore: OriginalDisconnectTransport, @unchecked Sendable {
        let deliveryBinding = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")
        private let lock = NSLock()
        private var count = 0
        var sent: Int { lock.withLock { count } }
        func deliver(input: Data, operationKey: String) async -> OriginalDisconnectOutcome {
            lock.withLock { count += 1 }
            return .definiteFailure(code: "conflict")
        }
    }

    private final class AdmittingTransport: CaptureDeliveryTransport, @unchecked Sendable {
        let descriptorDigest = "sha256:" + String(repeating: "c", count: 64)
        let deliveryBinding = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")
        func deliver(input: Data, payloadURL: URL, operationKey: String) async -> CaptureDeliveryOutcome {
            .admitted(captureID: "dcap-" + String(repeating: "b", count: 64))
        }
    }

    // MARK: - The lost acknowledgement

    /// The person disconnects a watched file. Core commits the disconnect and the answer is
    /// lost. The companion restarts. The revocation is still there, no second intent
    /// exists, the source is not rediscovered, and the reconciliation that completes it is
    /// the identical request under the identical key — which Core answers without acting
    /// twice.
    func testALostDisconnectAcknowledgementKeepsTheRevocationAndReconcilesOnTheSameKey() async throws {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("watched.txt")
        try Data("baseline".utf8).write(to: original)
        let stateRoot = root.appendingPathComponent("state", isDirectory: true)
        let clock = TestClock()
        let environment = try launch(stateRoot, clock)
        let watch = try await activeWatch(environment, original: original)

        // One capture is published and still queued when the disconnect happens.
        try Data("changed".utf8).write(to: original)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [watch.subscriptionID])
        clock.advance(3_000)
        _ = await environment.fileMonitoring.reconcileAll()
        let queued = try XCTUnwrap(
            environment.queue.allEntries().first { $0.intent.surface == .automaticFile }
        )
        XCTAssertEqual(queued.state, .pending)

        // Disconnect: revoked here at once, with the intent recorded in the same write.
        let intents = try await environment.disconnectSource(
            linkedDocumentID: watch.linkedDocumentID,
            fallback: .companion(workspaceRef: "workspace-1")
        )
        let intent = try XCTUnwrap(intents.first)
        XCTAssertEqual(intents.count, 1)
        XCTAssertEqual(intent.accessRef, watch.accessRef)
        XCTAssertEqual(intent.linkedDocumentID, watch.linkedDocumentID)
        XCTAssertEqual(intent.routeKind, "direct_file")
        XCTAssertEqual(intent.generation, watch.bindingGeneration)
        XCTAssertEqual(intent.binding, NativeRootAccessBinding(.companion(workspaceRef: "workspace-1")))
        XCTAssertEqual(
            intent.operationKey,
            OriginalDisconnectIntent.operationKey(
                workspaceRef: "workspace-1",
                accessRef: watch.accessRef,
                linkedDocumentID: watch.linkedDocumentID,
                generation: watch.bindingGeneration
            )
        )
        XCTAssertEqual(environment.documentAccessRoutes().map(\.state), [.disconnected])
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.stopped])
        let refusal = try failure {
            try environment.accessStore.withReadAccess(
                accessRef: watch.accessRef,
                expectedGeneration: watch.bindingGeneration
            ) { _ in Data() }
        }
        XCTAssertEqual(refusal as? NativeAccessError, .disconnected)

        // Core commits; the acknowledgement never arrives.
        let core = CommittingCore()
        core.loseNextAcknowledgements(1)
        let first = await environment.originalDisconnectReconciler(transport: core).reconcilePending()
        XCTAssertEqual(first, [.owed(operationKey: intent.operationKey)])
        XCTAssertEqual(core.mutations, 1)
        XCTAssertEqual(environment.accessStore.pendingOriginalDisconnects(), [intent])

        // The companion restarts over the same durable state.
        let restarted = try launch(stateRoot, clock)
        XCTAssertEqual(restarted.accessStore.pendingOriginalDisconnects(), [intent])
        let revoked = try XCTUnwrap(restarted.documentAccessRoutes().first)
        XCTAssertEqual(revoked.state, .disconnected)
        XCTAssertEqual(revoked.generation, watch.bindingGeneration)
        let afterRestart = try failure {
            try restarted.accessStore.withReadAccess(
                accessRef: watch.accessRef,
                expectedGeneration: watch.bindingGeneration
            ) { _ in Data() }
        }
        XCTAssertEqual(afterRestart as? NativeAccessError, .disconnected)

        // Nothing rediscovers or resurrects the source, however often it is checked: no
        // second grant, no new generation, no new intent, no new queued work.
        try Data("changed again".utf8).write(to: original)
        clock.advance(3_000)
        _ = await restarted.fileMonitoring.reconcileAll()
        _ = await restarted.folderMonitoring.reconcileAll()
        XCTAssertEqual(restarted.documentAccessRoutes(), [revoked])
        XCTAssertEqual(
            restarted.queue.allEntries().filter { $0.intent.surface == .automaticFile }.map(\.intent.itemId),
            [queued.intent.itemId]
        )
        XCTAssertEqual(restarted.accessStore.pendingOriginalDisconnects(), [intent])
        // Disconnecting it again records nothing: there is no connected route left.
        let again = try await restarted.disconnectSource(
            linkedDocumentID: watch.linkedDocumentID,
            fallback: .companion(workspaceRef: "workspace-1")
        )
        XCTAssertEqual(again, [])
        XCTAssertEqual(restarted.accessStore.pendingOriginalDisconnects(), [intent])

        // The reconciler built on the restarted state completes it: the same request, the
        // same key, byte for byte.
        let second = await restarted.originalDisconnectReconciler(transport: core).reconcilePending()
        XCTAssertEqual(second, [.acknowledged(operationKey: intent.operationKey)])
        let sent = core.received
        XCTAssertEqual(sent.count, 2)
        XCTAssertEqual(sent[0].key, intent.operationKey)
        XCTAssertEqual(sent[1].key, sent[0].key)
        XCTAssertEqual(sent[1].input, sent[0].input)
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: sent[0].input) as? [String: Any])
        XCTAssertEqual(
            body as NSDictionary,
            [
                "document_id": watch.linkedDocumentID,
                "owner_ref": watch.accessRef,
                "route_kind": "direct_file",
            ] as NSDictionary
        )
        // Core acted once; the replay did not make a second change.
        XCTAssertEqual(core.mutations, 1)
        XCTAssertEqual(restarted.accessStore.pendingOriginalDisconnects(), [])
        let settled = try launch(stateRoot, clock)
        XCTAssertEqual(settled.accessStore.pendingOriginalDisconnects(), [])
        XCTAssertEqual(settled.documentAccessRoutes(), [revoked])
        let quiet = await settled.originalDisconnectReconciler(transport: core).reconcilePending()
        XCTAssertEqual(quiet, [])
        XCTAssertEqual(core.received.count, 2)

        // The addition that was already queued stays, and is still delivered: Disconnect
        // does not cancel published work.
        let delivered = await CaptureDeliveryCoordinator(
            queue: settled.queue,
            transport: AdmittingTransport()
        ).deliverPending()
        XCTAssertEqual(delivered.map(\.state), [.admitted])
        XCTAssertEqual(delivered.first?.intent.itemId, queued.intent.itemId)
        XCTAssertEqual(settled.documentAccessRoutes(), [revoked])
    }

    /// Core being unreachable is not a reason to wait, and not a reason to restore anything:
    /// the revocation holds, the intent waits unchanged, and no pass allocates a key.
    func testAnUnreachableCoreLeavesTheRevocationAndTheIntentUntouchedUntilItReturns() async throws {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("note.txt")
        try Data("baseline".utf8).write(to: original)
        let environment = try launch(root.appendingPathComponent("state"), TestClock())
        let watch = try await activeWatch(environment, original: original)
        let revocation = try await environment.disconnectSource(
            linkedDocumentID: watch.linkedDocumentID,
            fallback: .companion(workspaceRef: "workspace-1")
        )
        let intent = try XCTUnwrap(revocation.first)

        let core = CommittingCore()
        core.setOnline(false)
        let reconciler = environment.originalDisconnectReconciler(transport: core)
        for _ in 0..<3 {
            let pass = await reconciler.reconcilePending()
            XCTAssertEqual(pass, [.owed(operationKey: intent.operationKey)])
        }
        XCTAssertEqual(core.received.count, 0)
        XCTAssertEqual(environment.accessStore.pendingOriginalDisconnects(), [intent])
        XCTAssertEqual(environment.documentAccessRoutes().map(\.state), [.disconnected])

        core.setOnline(true)
        let pass = await reconciler.reconcilePending()
        XCTAssertEqual(pass, [.acknowledged(operationKey: intent.operationKey)])
        XCTAssertEqual(core.received.map(\.key), [intent.operationKey])
        XCTAssertEqual(core.mutations, 1)
    }

    /// An intent is closed over the identity it was made under. Another account or target
    /// is not a substitute for it, and the intent is not rewritten to match.
    func testAnIntentFrozenForAnotherIdentityIsNeverSentUnderTheCurrentOne() async throws {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("note.txt")
        try Data("baseline".utf8).write(to: original)
        let environment = try launch(root.appendingPathComponent("state"), TestClock())
        let watch = try await activeWatch(environment, original: original)
        let revocation = try await environment.disconnectSource(
            linkedDocumentID: watch.linkedDocumentID,
            fallback: .companion(workspaceRef: "workspace-1")
        )
        let intent = try XCTUnwrap(revocation.first)

        let other = CommittingCore(binding: .companion(workspaceRef: "workspace-2"))
        let pass = await environment.originalDisconnectReconciler(transport: other).reconcilePending()
        XCTAssertEqual(pass, [.owed(operationKey: intent.operationKey)])
        XCTAssertEqual(other.received.count, 0)
        XCTAssertEqual(environment.accessStore.pendingOriginalDisconnects(), [intent])
    }

    /// A refusal no retry can change is recorded and not sent again. The local revocation
    /// is not undone to match Core's stale state.
    func testAPermanentRefusalIsRecordedAndNotRetriedAndNeverRestoresTheGrant() async throws {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("note.txt")
        try Data("baseline".utf8).write(to: original)
        let stateRoot = root.appendingPathComponent("state")
        let environment = try launch(stateRoot, TestClock())
        let watch = try await activeWatch(environment, original: original)
        let revocation = try await environment.disconnectSource(
            linkedDocumentID: watch.linkedDocumentID,
            fallback: .companion(workspaceRef: "workspace-1")
        )
        let intent = try XCTUnwrap(revocation.first)
        let core = RefusingCore()
        let reconciler = environment.originalDisconnectReconciler(transport: core)
        let pass = await reconciler.reconcilePending()
        XCTAssertEqual(pass, [.refused(operationKey: intent.operationKey, code: "conflict")])
        let again = await reconciler.reconcilePending()
        XCTAssertEqual(again, [])
        XCTAssertEqual(core.sent, 1)
        let restarted = try launch(stateRoot, TestClock())
        XCTAssertEqual(restarted.accessStore.pendingOriginalDisconnects().map(\.refusalCode), ["conflict"])
        XCTAssertEqual(restarted.documentAccessRoutes().map(\.state), [.disconnected])
    }

    // MARK: - The control

    /// The window's Disconnect control: the design's confirmation, the production owner
    /// behind it, and the entry leaving the Files tab.
    @MainActor
    func testTheWindowDisconnectRevokesTheFileAndRemovesItFromTheFilesTab() async throws {
        XCTAssertEqual(
            FolderManagementWindowController.disconnectConfirmation,
            "Disconnect this file in this workspace on this Mac? Core will stop reading it "
                + "through all matching links here, including watched folders. Captured "
                + "versions and already queued additions remain. Your original file will not change."
        )
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("note.txt")
        try Data("baseline".utf8).write(to: original)
        let environment = try launch(root.appendingPathComponent("state"), TestClock())
        let watch = try await activeWatch(environment, original: original)
        let file = FilesAndFoldersItem.file(try XCTUnwrap(environment.fileSubscriptions.all().first))
        XCTAssertTrue(FilesAndFoldersModel.actions(for: file).disconnectEnabled)
        XCTAssertFalse(FilesAndFoldersModel.actions(for: nil).disconnectEnabled)

        // No owner wired: nothing is offered and nothing changes.
        let unwired = window(environment, disconnect: nil)
        let refused = await unwired.applyDisconnect(file)
        XCTAssertFalse(refused)
        XCTAssertEqual(environment.documentAccessRoutes().map(\.state), [.connected])

        let controller = window(environment) { value in
            (try? await environment.disconnectSource(
                linkedDocumentID: value.linkedDocumentID,
                fallback: .companion(workspaceRef: "workspace-1")
            )) != nil
        }
        controller.reload()
        XCTAssertEqual(controller.model.files.map(\.accessRef), [watch.accessRef])
        let revoked = await controller.applyDisconnect(file)
        XCTAssertTrue(revoked)
        XCTAssertEqual(controller.model.files, [])
        XCTAssertEqual(environment.documentAccessRoutes().map(\.state), [.disconnected])
        XCTAssertEqual(environment.accessStore.pendingOriginalDisconnects().map(\.accessRef), [watch.accessRef])
        // Only a file row can be disconnected from here.
        let folderOnly = await controller.applyDisconnect(
            .retainedRoot(RetainedRootGroup(
                rootRef: "root-1", displayName: "Folder", memberCount: 0, membersFrozen: true
            ))
        )
        XCTAssertFalse(folderOnly)
    }

    // MARK: - Folder members

    /// A watched-folder member is a route of the same source: it is revoked with it, and
    /// the intent names the member route under its root's frozen workspace. The folder keeps
    /// the member on record so a later scan never re-enrols it.
    func testAFolderMemberRouteIsDisconnectedAndItsIntentNamesTheRootAndMember() throws {
        let root = try temporaryDirectory()
        let folder = root.appendingPathComponent("folder", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try Data("a".utf8).write(to: folder.appendingPathComponent("a.txt"))
        try Data("b".utf8).write(to: root.appendingPathComponent("b.txt"))
        let state = root.appendingPathComponent("access", isDirectory: true)
        let store = try NativeAccessStore(stateDirectory: state, codec: BookmarkCodec())
        let binding = NativeRootAccessBinding(.companion(workspaceRef: "workspace-9"))
        let group = try store.adoptRoot(
            rootRef: "root-a", ownerRef: "run-a", selectedURL: folder, binding: binding
        )
        _ = try store.adoptRootMember(
            accessRef: "owner-a", linkedDocumentID: "doc-a", rootRef: "root-a",
            memberRef: "member-a", relativeComponents: ["a.txt"], expectedRootGeneration: group.generation
        )
        _ = try store.adopt(
            accessRef: "owner-b", linkedDocumentID: "doc-a",
            selectedURL: root.appendingPathComponent("b.txt"), route: .directFile
        )
        _ = try store.adopt(
            accessRef: "owner-c", linkedDocumentID: "doc-b",
            selectedURL: root.appendingPathComponent("b.txt"), route: .directFile
        )

        let fallback = NativeRootAccessBinding(.companion(workspaceRef: "workspace-1"))
        let intents = try store.disconnectSource(linkedDocumentID: "doc-a", fallback: fallback)

        XCTAssertEqual(intents.map(\.accessRef), ["owner-a", "owner-b"])
        let member = try XCTUnwrap(intents.first { $0.accessRef == "owner-a" })
        XCTAssertEqual(member.routeKind, "root_member")
        XCTAssertEqual(member.rootRef, "root-a")
        XCTAssertEqual(member.memberRef, "member-a")
        XCTAssertEqual(member.binding, binding)
        let direct = try XCTUnwrap(intents.first { $0.accessRef == "owner-b" })
        XCTAssertEqual(direct.routeKind, "direct_file")
        XCTAssertNil(direct.rootRef)
        XCTAssertEqual(direct.binding, fallback)
        XCTAssertNotEqual(member.operationKey, direct.operationKey)
        XCTAssertEqual(
            Dictionary(uniqueKeysWithValues: store.descriptors().map { ($0.accessRef, $0.state) }),
            ["owner-a": .disconnected, "owner-b": .disconnected, "owner-c": .connected]
        )
        XCTAssertEqual(
            try OriginalDisconnectWireEncoder().encode(member),
            Data(#"{"document_id":"doc-a","member_ref":"member-a","owner_ref":"owner-a","root_ref":"root-a","route_kind":"root_member"}"#.utf8)
        )

        // Both survive a reopen, and the unrelated document is untouched.
        let reopened = try NativeAccessStore(stateDirectory: state, codec: BookmarkCodec())
        XCTAssertEqual(reopened.pendingOriginalDisconnects(), intents.sorted { $0.operationKey < $1.operationKey })
    }

    /// A state file written before Disconnect reached Core has no intents and reads as empty;
    /// one whose intent could not have been written by this build is refused, not sent.
    func testAnIntentThatCouldNotHaveBeenWrittenIsRefusedWhenTheStoreIsRead() throws {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("note.txt")
        try Data("x".utf8).write(to: original)
        let state = root.appendingPathComponent("access", isDirectory: true)
        let store = try NativeAccessStore(stateDirectory: state, codec: BookmarkCodec())
        _ = try store.adopt(
            accessRef: "owner-a", linkedDocumentID: "doc-a", selectedURL: original, route: .directFile
        )
        XCTAssertEqual(store.pendingOriginalDisconnects(), [])
        _ = try store.disconnectSource(
            linkedDocumentID: "doc-a", fallback: NativeRootAccessBinding(.companion(workspaceRef: "w"))
        )
        let file = state.appendingPathComponent("native-access-v1.json")
        let text = try String(contentsOf: file, encoding: .utf8)
        // A key that is not the one the intent's own fields derive.
        let key = try XCTUnwrap(store.pendingOriginalDisconnects().first?.operationKey)
        try text.replacingOccurrences(of: key, with: "disconnect-forged")
            .write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        XCTAssertThrowsError(try NativeAccessStore(stateDirectory: state, codec: BookmarkCodec())) {
            XCTAssertEqual($0 as? NativeAccessError, .invalidState)
        }
    }

    // MARK: - The CLI transport

    func testTheCLIAcknowledgesOnlyAnAnswerThatStatesTheBindingDisconnected() {
        func outcome(_ status: Int32, _ body: String, _ diagnostic: String = "") -> OriginalDisconnectOutcome {
            CLICaptureDelivery.disconnectOutcome(
                status: status, output: Data(body.utf8), diagnostic: Data(diagnostic.utf8)
            )
        }
        XCTAssertEqual(
            outcome(0, #"{"result":{"document_id":"d","owner_ref":"o","binding_generation":1,"connection_state":"disconnected"}}"#),
            .acknowledged(documentID: "d", ownerRef: "o")
        )
        // A success that does not say disconnected, or names nothing, acknowledges nothing.
        XCTAssertEqual(outcome(0, #"{"result":{"document_id":"d","owner_ref":"o","connection_state":"connected"}}"#), .unknown)
        XCTAssertEqual(outcome(0, #"{"result":{}}"#), .unknown)
        XCTAssertEqual(outcome(0, "not json"), .unknown)
        // A refusal is a refusal; Core's own "cannot re-serve" is unknown; access is held.
        XCTAssertEqual(outcome(1, #"{"error":{"code":"conflict"}}"#), .definiteFailure(code: "conflict"))
        XCTAssertEqual(
            outcome(1, #"{"error":{"code":"internal_recoverable","details":{"outcome_established":false}}}"#),
            .unknown
        )
        XCTAssertEqual(outcome(1, #"{"error":{"code":"authorization_denied"}}"#), .held(.needsAccess))
        XCTAssertEqual(outcome(1, #"{"error":{"code":"upgrade_required"}}"#), .held(.updateRequired))
        // The CLI's fixed refusals before anything is sent.
        XCTAssertEqual(outcome(1, "", CLICaptureDelivery.notRunning), .notSent)
        XCTAssertEqual(outcome(3, "", CLICaptureDelivery.credentialRefused), .held(.needsAccess))
        XCTAssertEqual(outcome(2, "", CLICaptureDelivery.refusedBeforeSend), .definiteFailure(code: "refused_before_send"))
        XCTAssertEqual(outcome(1, "", "something else"), .unknown)
    }

    func testTheCLICommandIsAttachOnlyUnderTheFrozenPrincipalWithTheIntentsKey() {
        let binding = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")
        let delivery = CLICaptureDelivery(
            executable: URL(fileURLWithPath: "/bin/omnivia"),
            installationState: URL(fileURLWithPath: "/state"),
            deliveryBinding: binding
        )
        XCTAssertEqual(
            delivery.disconnectArguments(operationKey: "disconnect-k"),
            [
                "--require-running", "--installation-state", "/state",
                "--workspace-id", "workspace-1", "--timeout-ms", "20000",
                "document", "disconnect", "--input-stdin",
                "--idempotency-key", "disconnect-k",
                "--principal", "local-user", "--json",
            ]
        )
    }

    // MARK: - Fixtures

    private struct Watch {
        let subscriptionID: String
        let accessRef: String
        let linkedDocumentID: String
        let bindingGeneration: UInt64
    }

    private func launch(_ stateRoot: URL, _ clock: TestClock) throws -> DocumentIntakeEnvironment {
        try DocumentIntakeEnvironment(
            stateRoot: stateRoot,
            bookmarkCodec: BookmarkCodec(),
            monotonicClock: clock.reader
        )
    }

    /// One admitted baseline watch over `original`, as the manual Add that establishes it
    /// leaves things.
    private func activeWatch(_ environment: DocumentIntakeEnvironment, original: URL) async throws -> Watch {
        let baseline = try await ManualCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        ).submit([ManualCaptureCandidate(url: original, keepUpdated: true)])
        let digest = "sha256:" + String(repeating: "d", count: 64)
        let staging = try environment.queue.beginStage(
            itemID: baseline[0].intent.itemId,
            requestDigest: digest,
            descriptorDigest: digest
        )
        _ = try environment.queue.markAdmitted(
            itemID: baseline[0].intent.itemId,
            attemptGeneration: staging.attemptGeneration,
            captureID: "dcap-" + String(repeating: "a", count: 64)
        )
        _ = await environment.fileMonitoring.reconcileReceipts()
        let value = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(value.state, .active)
        return Watch(
            subscriptionID: value.subscriptionID,
            accessRef: value.accessRef,
            linkedDocumentID: value.linkedDocumentID,
            bindingGeneration: value.bindingGeneration
        )
    }

    @MainActor
    private func window(
        _ environment: DocumentIntakeEnvironment,
        disconnect: ((FileSubscription) async -> Bool)?
    ) -> FolderManagementWindowController {
        FolderManagementWindowController(
            workspaceName: "workspace-1",
            folderStore: environment.folderSubscriptions,
            folderMonitoring: environment.folderMonitoring,
            fileStore: environment.fileSubscriptions,
            fileMonitoring: environment.fileMonitoring,
            bulkStore: environment.folderBulkRuns,
            bulkCoordinator: environment.folderBulkCoordinator,
            activityStore: environment.captureActivity,
            observeActivity: { try environment.recordCaptureActivity() },
            rootGroups: { environment.rootAccessGroups() },
            documentRoutes: { environment.documentAccessRoutes() },
            disconnectSource: disconnect
        )
    }

    @MainActor
    private func window(
        _ environment: DocumentIntakeEnvironment,
        _ disconnect: @escaping (FileSubscription) async -> Bool
    ) -> FolderManagementWindowController {
        window(environment, disconnect: disconnect)
    }

    private func failure<T>(
        file: StaticString = #filePath,
        line: UInt = #line,
        _ body: () throws -> T
    ) throws -> Error {
        do {
            _ = try body()
            XCTFail("expected a failure", file: file, line: line)
            throw CaptureDeliveryError.invalidIntent
        } catch {
            return error
        }
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-original-disconnect-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
