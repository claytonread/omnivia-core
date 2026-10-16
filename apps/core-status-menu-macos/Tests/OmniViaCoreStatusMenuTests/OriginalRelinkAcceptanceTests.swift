import CryptoKit
import Foundation
import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

/// AT-27 · Locate during preparation (design §9.5).
///
/// Every test drives the production path: `DocumentIntakeEnvironment.stageRelink` and
/// `relinkSource` (what the Files tab's Locate control reaches through `OriginalRelinkFlow`),
/// the durable `NativeAccessStore` intent and `OriginalRelinkReconciler`. The only stand-in is
/// Core itself. `CommittingCore` models exactly one property of Core's
/// `document.original.relink` operation: a key it has committed answers a second time with
/// the same result and does not act again. The live arm in `OriginalRelinkLiveCoreTests`
/// removes the stand-in and runs the real CLI against a real service.
final class OriginalRelinkAcceptanceTests: XCTestCase {
    /// A bookmark is its path, and every resolution is recorded: a path that was never
    /// resolved was never read through a grant.
    private final class LoggingCodec: SecurityScopedBookmarkCoding, @unchecked Sendable {
        private let lock = NSLock()
        private var paths: [String] = []
        var resolved: [String] { lock.withLock { paths } }
        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            let path = String(decoding: data, as: UTF8.self)
            lock.withLock { paths.append(URL(fileURLWithPath: path).lastPathComponent) }
            return (URL(fileURLWithPath: path), false)
        }
        func startAccessing(_ url: URL) -> Bool { true }
        func stopAccessing(_ url: URL) {}
    }

    /// Every read the intake makes, by file name, whichever way it reached the file.
    private final class RecordingReader: CoordinatedOriginalReading, @unchecked Sendable {
        private let lock = NSLock()
        private var names: [String] = []
        var reads: [String] { lock.withLock { names } }
        func readStableBytes(from url: URL, maximumBytes: UInt64) throws -> Data {
            lock.withLock { names.append(url.lastPathComponent) }
            return try Data(contentsOf: url)
        }
    }

    private final class TestClock: @unchecked Sendable {
        private let lock = NSLock()
        private var value: Int64 = 0
        func advance(_ delta: Int64) { lock.withLock { value += delta } }
        var reader: @Sendable () -> Int64 { { [self] in self.lock.withLock { self.value } } }
    }

    private final class OperationLog: @unchecked Sendable {
        private let lock = NSLock()
        private var entries: [String] = []
        func append(_ value: String) { lock.withLock { entries.append(value) } }
        var all: [String] { lock.withLock { entries } }
    }

    /// Core's side of an idempotent mutation, and nothing else: the first request under a
    /// key acts, any later request under it answers the stored result, and a different body
    /// under a used key is refused. `loseAcknowledgements` drops the answer after the commit.
    private final class CommittingCore: OriginalRelinkTransport, @unchecked Sendable {
        let deliveryBinding: CaptureDeliveryBinding
        private let lock = NSLock()
        private var stored: [String: Data] = [:]
        private var lost = 0
        private var online = true
        private var requests: [(key: String, input: Data)] = []
        private var acts = 0
        private let log: OperationLog?
        /// What the next answer claims, to prove an answer about something else is not taken.
        private var answerOverride: (documentID: String?, ownerRef: String?, generation: UInt64?)?
        /// Core's head capture, which a relink's answer reports.
        var head: String?

        init(
            binding: CaptureDeliveryBinding = .companion(workspaceRef: "workspace-1"),
            log: OperationLog? = nil
        ) {
            deliveryBinding = binding
            self.log = log
        }

        var received: [(key: String, input: Data)] { lock.withLock { requests } }
        /// How many times Core actually changed a binding.
        var mutations: Int { lock.withLock { acts } }
        func loseNextAcknowledgements(_ count: Int) { lock.withLock { lost = count } }
        func setOnline(_ value: Bool) { lock.withLock { online = value } }
        func answerAbout(documentID: String? = nil, ownerRef: String? = nil, generation: UInt64? = nil) {
            lock.withLock { answerOverride = (documentID, ownerRef, generation) }
        }

        func deliverRelink(input: Data, operationKey: String) async -> OriginalRelinkOutcome {
            lock.withLock {
                guard online else { return .notSent }
                requests.append((operationKey, input))
                log?.append("relink")
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
                      let ownerRef = body["owner_ref"] as? String,
                      let generation = (body["binding_generation"] as? NSNumber)?.uint64Value
                else { return .definiteFailure(code: "invalid_request") }
                return .acknowledged(
                    documentID: answerOverride?.documentID ?? documentID,
                    ownerRef: answerOverride?.ownerRef ?? ownerRef,
                    bindingGeneration: answerOverride?.generation ?? generation,
                    currentCaptureID: head
                )
            }
        }
    }

    private final class DisconnectingCore: OriginalDisconnectTransport, @unchecked Sendable {
        let deliveryBinding = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")
        private let log: OperationLog
        init(log: OperationLog) { self.log = log }
        func deliver(input: Data, operationKey: String) async -> OriginalDisconnectOutcome {
            log.append("disconnect")
            guard let body = (try? JSONSerialization.jsonObject(with: input)) as? [String: Any],
                  let documentID = body["document_id"] as? String,
                  let ownerRef = body["owner_ref"] as? String
            else { return .definiteFailure(code: "invalid_request") }
            return .acknowledged(documentID: documentID, ownerRef: ownerRef)
        }
    }

    private final class RefusingCore: OriginalRelinkTransport, @unchecked Sendable {
        let deliveryBinding = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")
        private let lock = NSLock()
        private var count = 0
        var sent: Int { lock.withLock { count } }
        func deliverRelink(input: Data, operationKey: String) async -> OriginalRelinkOutcome {
            lock.withLock { count += 1 }
            return .definiteFailure(code: "conflict")
        }
    }

    private struct World {
        let root: URL
        let stateRoot: URL
        let original: URL
        let chosen: URL
        let codec: LoggingCodec
        let reader: RecordingReader
        let clock: TestClock
        let environment: DocumentIntakeEnvironment
        let watch: FileSubscription
    }

    private let fallback = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")

    // MARK: - AT-27: the race

    /// AT-27. A watched file's refresh has read its original and written its complete
    /// candidate when the person confirms a Locate to a different file. The relink wins:
    /// the reserved generation makes the final validation refuse the whole batch, so the
    /// stale association publishes nothing — not what it read, and above all not the file
    /// that was just chosen. Neither the old grant nor the candidate's is resolved until
    /// Core confirms the generation; only then does the new grant read, under the new
    /// generation, and the watch stays paused until the person resumes it.
    func testALocateThatWinsAfterThePreparationReadItsOriginalPublishesNothingUnderTheOldAssociation()
        async throws
    {
        let world = try await makeWorld()
        let environment = world.environment
        let watch = world.watch
        let baselineEntries = environment.queue.allEntries().map(\.intent.itemId)

        // The refresh starts: the original changed and its quiet period passed.
        try Data("a newer version".utf8).write(to: world.original)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [watch.subscriptionID])
        world.clock.advance(3_000)

        // The confirmed Locate lands after the whole candidate is prepared and before the
        // publication critical section — the last point at which it can still win.
        var reserved: OriginalRelinkIntent?
        var staged: OriginalRelinkCandidate?
        environment.preparation.afterCandidatePrepared = {
            do {
                let candidate = try environment.stageRelink(
                    for: watch, selectedURL: world.chosen, baseline: nil
                )
                staged = candidate
                reserved = try environment.relinkSource(candidate: candidate, fallback: self.fallback)
            } catch {
                XCTFail("Locate failed inside the race: \(error)")
            }
        }
        let reports = await environment.fileMonitoring.reconcileAll()
        environment.preparation.afterCandidatePrepared = nil

        let intent = try XCTUnwrap(reserved)
        XCTAssertEqual(intent.generation, watch.bindingGeneration + 1)
        XCTAssertEqual(staged?.comparison, .noCapturedVersion)

        // Nothing was published: no entry beyond the baseline, no payload of either file.
        XCTAssertEqual(environment.queue.allEntries().map(\.intent.itemId), baselineEntries)
        for entry in environment.queue.allEntries() {
            let payload = try? Data(contentsOf: environment.queue.payloadURL(for: entry.intent.itemId))
            XCTAssertNotEqual(payload, Data("the file Locate chose".utf8))
            XCTAssertNotEqual(payload, Data("a newer version".utf8))
        }
        XCTAssertEqual(reports.map(\.queued), [false])
        // The refused read did not turn the pause into a loss of access.
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.paused])

        // The route is paused on the reserved generation and holds no grant at all.
        let route = try XCTUnwrap(environment.documentAccessRoutes().first)
        XCTAssertEqual(route.state, .relinking)
        XCTAssertEqual(route.generation, intent.generation)
        for generation in [watch.bindingGeneration, intent.generation] {
            let refusal = try failure {
                try environment.accessStore.withReadAccess(
                    accessRef: watch.accessRef, expectedGeneration: generation
                ) { _ in Data() }
            }
            XCTAssertEqual(refusal as? NativeAccessError, .relinkPending)
        }
        // The chosen file was read once, to compare it, and never through a grant.
        XCTAssertEqual(world.reader.reads.filter { $0 == "chosen.txt" }, ["chosen.txt"])
        XCTAssertFalse(world.codec.resolved.contains("chosen.txt"))

        // Resume cannot begin before Core has answered.
        let resumed = try await resumeOutcome(environment, watch)
        XCTAssertEqual(resumed as? NativeAccessError, .relinkPending)
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.paused])

        // Core confirms the generation. Only now does the candidate become the grant.
        let core = CommittingCore()
        let pass = await environment.originalRelinkReconciler(transport: core).reconcilePending()
        XCTAssertEqual(pass, [.acknowledged(operationKey: intent.operationKey)])
        XCTAssertEqual(core.mutations, 1)
        XCTAssertEqual(environment.accessStore.pendingOriginalRelinks(), [])
        let activated = try XCTUnwrap(environment.documentAccessRoutes().first)
        XCTAssertEqual(activated.state, .connected)
        XCTAssertEqual(activated.generation, intent.generation)
        let moved = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(moved.bindingGeneration, intent.generation)
        XCTAssertEqual(moved.state, .paused, "automatic updates stay paused after the relink")
        XCTAssertEqual(moved.linkedDocumentID, watch.linkedDocumentID)
        // The superseded generation can never read again.
        let stale = try failure {
            try environment.accessStore.withReadAccess(
                accessRef: watch.accessRef, expectedGeneration: watch.bindingGeneration
            ) { _ in Data() }
        }
        XCTAssertEqual(stale as? NativeAccessError, .generationMismatch)

        // Nothing has digested the selected version yet; the explicit Resume does, under the
        // new generation, for the same document.
        XCTAssertEqual(environment.queue.allEntries().map(\.intent.itemId), baselineEntries)
        _ = try await environment.fileMonitoring.resume(subscriptionID: watch.subscriptionID)
        world.clock.advance(3_000)
        _ = await environment.fileMonitoring.reconcileAll()
        let published = environment.queue.allEntries().filter { !baselineEntries.contains($0.intent.itemId) }
        XCTAssertEqual(published.count, 1)
        let entry = try XCTUnwrap(published.first)
        XCTAssertEqual(
            try Data(contentsOf: environment.queue.payloadURL(for: entry.intent.itemId)),
            Data("the file Locate chose".utf8)
        )
        XCTAssertEqual(entry.intent.originalBinding?.bindingGeneration, intent.generation)
        XCTAssertEqual(entry.intent.originalBinding?.accessRecordId, watch.accessRef)
        XCTAssertEqual(entry.intent.originalBinding?.linkedDocumentId, watch.linkedDocumentID)
        XCTAssertEqual(entry.intent.requestedSourceIdentity, watch.requestedSourceIdentity)
        XCTAssertTrue(world.codec.resolved.contains("chosen.txt"))
    }

    /// The other order. The Locate is confirmed first, so a refresh that starts afterwards
    /// is refused before it reads: the old file is not read under the old generation, and
    /// the new one is not read at all.
    func testARefreshThatStartsAfterALocateIsRefusedBeforeItReadsEitherFile() async throws {
        let world = try await makeWorld()
        let environment = world.environment
        let watch = world.watch
        let candidate = try environment.stageRelink(for: watch, selectedURL: world.chosen, baseline: nil)
        try environment.relinkSource(candidate: candidate, fallback: fallback)
        let readsBefore = world.reader.reads
        let resolvedBefore = world.codec.resolved

        // The pause above is the watch's own; a stale check still holding a claim, or an
        // explicit Check now, must hit the grant.
        let entries = try await failureAsync {
            try await environment.preparation.prepareAndPublish(
                [self.refreshItem(for: watch, generation: watch.bindingGeneration)],
                binding: self.fallback
            )
        }
        XCTAssertEqual(entries as? NativeAccessError, .relinkPending)
        XCTAssertEqual(world.reader.reads, readsBefore)
        XCTAssertEqual(world.codec.resolved, resolvedBefore)
        XCTAssertEqual(environment.queue.allEntries().count, 1, "only the baseline")
    }

    // MARK: - Historical captures

    /// A refresh published under the old generation can reach Core after the relink and be
    /// admitted as history. History is preserved, and it is never the base of the next
    /// refresh: the watch is re-based on the head Core reports with its confirmation, and an
    /// old-generation receipt that arrives afterwards adopts nothing.
    func testAnOldGenerationRefreshAdmittedAsHistoryIsNeverTheBaseOfTheNextRefresh() async throws {
        let world = try await makeWorld()
        let environment = world.environment
        let watch = world.watch
        let head = "dcap-" + String(repeating: "b", count: 64)
        let late = "dcap-" + String(repeating: "c", count: 64)

        // A refresh is published under generation one and is still queued when Locate lands.
        try Data("a newer version".utf8).write(to: world.original)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [watch.subscriptionID])
        world.clock.advance(3_000)
        _ = await environment.fileMonitoring.reconcileAll()
        let queued = try XCTUnwrap(
            environment.queue.allEntries().first { $0.intent.surface == .automaticFile }
        )
        XCTAssertEqual(queued.intent.originalBinding?.bindingGeneration, watch.bindingGeneration)

        let candidate = try environment.stageRelink(for: watch, selectedURL: world.chosen, baseline: nil)
        let intent = try environment.relinkSource(candidate: candidate, fallback: fallback)
        let core = CommittingCore()
        core.head = head
        let pass = await environment.originalRelinkReconciler(transport: core).reconcilePending()
        XCTAssertEqual(pass, [.acknowledged(operationKey: intent.operationKey)])
        let rebased = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(rebased.bindingGeneration, intent.generation)
        XCTAssertEqual(rebased.currentSuccessfulCaptureID, head)

        // Core takes the queued refresh as history, and the receipt reaches this Mac.
        let digest = "sha256:" + String(repeating: "e", count: 64)
        let staging = try environment.queue.beginStage(
            itemID: queued.intent.itemId, requestDigest: digest, descriptorDigest: digest
        )
        _ = try environment.queue.markAdmitted(
            itemID: queued.intent.itemId, attemptGeneration: staging.attemptGeneration, captureID: late
        )
        _ = await environment.fileMonitoring.reconcileReceipts()
        let after = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(after.currentSuccessfulCaptureID, head, "history is not the base")
        XCTAssertNil(after.lastQueuedItemID, "the receipt itself is still accounted for")

        // The next refresh, read from the new file under the new generation, names Core's head.
        _ = try await environment.fileMonitoring.resume(subscriptionID: watch.subscriptionID)
        world.clock.advance(3_000)
        _ = await environment.fileMonitoring.reconcileAll()
        let next = try XCTUnwrap(
            environment.queue.allEntries().first {
                $0.intent.surface == .automaticFile && $0.intent.itemId != queued.intent.itemId
            }
        )
        XCTAssertEqual(next.intent.originalBinding?.bindingGeneration, intent.generation)
        XCTAssertEqual(next.intent.basePrecondition, .expected(head))
    }

    /// A stop between the reservation and the pause it is written with leaves the watch
    /// running with nothing to read. The confirmation still leaves it paused: automatic
    /// updates stay paused through a relink whichever way it got there.
    func testAConfirmationPausesAWatchTheReservationDidNotReach() async throws {
        let world = try await makeWorld()
        let environment = world.environment
        let candidate = try environment.stageRelink(for: world.watch, selectedURL: world.chosen, baseline: nil)
        // The store alone, as a stop after its write and before the pause would leave it.
        let intent = try environment.accessStore.reserveRelink(
            accessRef: world.watch.accessRef,
            selectedURL: candidate.url,
            binding: NativeRootAccessBinding(fallback)
        )
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.active])
        let pass = await environment.originalRelinkReconciler(transport: CommittingCore()).reconcilePending()
        XCTAssertEqual(pass, [.acknowledged(operationKey: intent.operationKey)])
        let moved = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(moved.state, .paused)
        XCTAssertEqual(moved.bindingGeneration, intent.generation)
    }

    // MARK: - Recovery

    /// Core commits the relink and the answer is lost. The companion restarts. The route is
    /// still paused on the reserved generation holding no grant, the intent is the same
    /// intent, and the reconciliation that completes it is the identical request under the
    /// identical key — which Core answers without acting twice. Only that answer activates
    /// the new grant.
    func testALostAcknowledgementKeepsTheRouteHeldAndReconcilesOnTheSameKeyAfterARestart() async throws {
        let world = try await makeWorld()
        let watch = world.watch
        let candidate = try world.environment.stageRelink(
            for: watch, selectedURL: world.chosen, baseline: nil
        )
        // A second candidate staged before the first is confirmed: staging binds nothing.
        let other = world.root.appendingPathComponent("other.txt")
        try Data("another".utf8).write(to: other)
        let second = try world.environment.stageRelink(for: watch, selectedURL: other, baseline: nil)
        let intent = try world.environment.relinkSource(candidate: candidate, fallback: fallback)
        XCTAssertEqual(
            intent.operationKey,
            OriginalRelinkIntent.operationKey(
                workspaceRef: "workspace-1",
                accessRef: watch.accessRef,
                linkedDocumentID: watch.linkedDocumentID,
                generation: watch.bindingGeneration + 1
            )
        )
        XCTAssertEqual(intent.binding, NativeRootAccessBinding(fallback))
        XCTAssertEqual(intent.candidateName, "chosen.txt")

        let core = CommittingCore()
        core.loseNextAcknowledgements(1)
        let first = await world.environment.originalRelinkReconciler(transport: core).reconcilePending()
        XCTAssertEqual(first, [.owed(operationKey: intent.operationKey)])
        XCTAssertEqual(core.mutations, 1)
        XCTAssertEqual(world.environment.documentAccessRoutes().map(\.state), [.relinking])

        // The companion restarts over the same durable state.
        let restartedCodec = LoggingCodec()
        let restarted = try launch(world.stateRoot, TestClock(), codec: restartedCodec)
        XCTAssertEqual(restarted.accessStore.pendingOriginalRelinks(), [intent])
        let route = try XCTUnwrap(restarted.documentAccessRoutes().first)
        XCTAssertEqual(route.state, .relinking)
        XCTAssertEqual(route.generation, intent.generation)
        XCTAssertEqual(restarted.fileSubscriptions.all().map(\.state), [.paused])
        XCTAssertEqual(
            restarted.fileSubscriptions.all().map(\.bindingGeneration), [watch.bindingGeneration],
            "the watch learns the generation only when Core has confirmed it"
        )
        let refusal = try failure {
            try restarted.accessStore.withReadAccess(
                accessRef: watch.accessRef, expectedGeneration: intent.generation
            ) { _ in Data() }
        }
        XCTAssertEqual(refusal as? NativeAccessError, .relinkPending)
        // Locating again while one is owed mints nothing: no alternation between candidates.
        XCTAssertThrowsError(try restarted.stageRelink(for: watch, selectedURL: other, baseline: nil)) {
            XCTAssertEqual($0 as? OriginalRelinkError, .notRelinkable)
        }
        XCTAssertThrowsError(try restarted.relinkSource(candidate: second, fallback: fallback)) {
            XCTAssertEqual($0 as? NativeAccessError, .relinkPending)
        }
        XCTAssertEqual(restarted.accessStore.pendingOriginalRelinks(), [intent])
        XCTAssertEqual(restartedCodec.resolved, [], "a restart reads nothing")

        let reconciled = await restarted.originalRelinkReconciler(transport: core).reconcilePending()
        XCTAssertEqual(reconciled, [.acknowledged(operationKey: intent.operationKey)])
        let sent = core.received
        XCTAssertEqual(sent.count, 2)
        XCTAssertEqual(sent[1].key, sent[0].key)
        XCTAssertEqual(sent[1].input, sent[0].input)
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: sent[0].input) as? [String: Any])
        XCTAssertEqual(
            body as NSDictionary,
            [
                "document_id": watch.linkedDocumentID,
                "owner_ref": watch.accessRef,
                "binding_generation": intent.generation,
            ] as NSDictionary,
            "the request names an owner, a document and a generation, never a path"
        )
        XCTAssertEqual(core.mutations, 1, "the replay did not make a second change")

        // And the activation itself survives another restart.
        let settled = try launch(world.stateRoot, TestClock(), codec: LoggingCodec())
        XCTAssertEqual(settled.accessStore.pendingOriginalRelinks(), [])
        let activated = try XCTUnwrap(settled.documentAccessRoutes().first)
        XCTAssertEqual(activated.state, .connected)
        XCTAssertEqual(activated.generation, intent.generation)
        XCTAssertEqual(settled.fileSubscriptions.all().map(\.bindingGeneration), [intent.generation])
        let url = try settled.accessStore.withReadAccess(
            accessRef: watch.accessRef, expectedGeneration: intent.generation
        ) { $0 }
        XCTAssertEqual(url.lastPathComponent, "chosen.txt")
        let quiet = await settled.originalRelinkReconciler(transport: core).reconcilePending()
        XCTAssertEqual(quiet, [])
        XCTAssertEqual(core.received.count, 2)
    }

    /// Nothing but Core's canonical answer about exactly this route and generation
    /// activates anything. Offline, unknown, held, foreign and wrongly-addressed answers all
    /// leave the intent owed under the same key and the route held.
    func testNoAnswerShortOfTheCanonicalConfirmationActivatesTheNewGrant() async throws {
        let world = try await makeWorld()
        let environment = world.environment
        let candidate = try environment.stageRelink(for: world.watch, selectedURL: world.chosen, baseline: nil)
        let intent = try environment.relinkSource(candidate: candidate, fallback: fallback)
        let key = intent.operationKey
        func assertHeld(_ message: String, file: StaticString = #filePath, line: UInt = #line) {
            XCTAssertEqual(environment.documentAccessRoutes().map(\.state), [.relinking], message, file: file, line: line)
            XCTAssertEqual(environment.accessStore.pendingOriginalRelinks(), [intent], file: file, line: line)
        }

        let core = CommittingCore()
        core.setOnline(false)
        let reconciler = environment.originalRelinkReconciler(transport: core)
        for _ in 0..<3 {
            let pass = await reconciler.reconcilePending()
            XCTAssertEqual(pass, [.owed(operationKey: key)])
        }
        XCTAssertEqual(core.received.count, 0)
        assertHeld("offline")

        // Another account or target is not a substitute for the one the intent froze.
        let foreign = CommittingCore(binding: .companion(workspaceRef: "workspace-2"))
        let foreignPass = await environment.originalRelinkReconciler(transport: foreign).reconcilePending()
        XCTAssertEqual(foreignPass, [.owed(operationKey: key)])
        XCTAssertEqual(foreign.received.count, 0)
        assertHeld("another identity")

        core.setOnline(true)
        for (label, override) in [
            ("another document", (documentID: Optional("doc-other"), ownerRef: Optional<String>.none, generation: Optional<UInt64>.none)),
            ("another owner", (documentID: nil, ownerRef: "access-other", generation: nil)),
            ("another generation", (documentID: nil, ownerRef: nil, generation: intent.generation + 1)),
            ("the superseded generation", (documentID: nil, ownerRef: nil, generation: intent.generation - 1)),
        ] {
            core.answerAbout(
                documentID: override.documentID, ownerRef: override.ownerRef, generation: override.generation
            )
            let pass = await reconciler.reconcilePending()
            XCTAssertEqual(pass, [.owed(operationKey: key)], label)
            assertHeld(label)
        }

        core.answerAbout()
        let pass = await reconciler.reconcilePending()
        XCTAssertEqual(pass, [.acknowledged(operationKey: key)])
        XCTAssertEqual(environment.documentAccessRoutes().map(\.state), [.connected])
    }

    /// A refusal no retry can change is recorded and not sent again. The route stays held —
    /// neither file is read — and only a refused relink may be replaced by another Locate,
    /// which reserves yet another generation under a key of its own.
    func testAPermanentRefusalIsRecordedKeepsTheRouteHeldAndMayBeReplacedByANewLocate() async throws {
        let world = try await makeWorld()
        let environment = world.environment
        let candidate = try environment.stageRelink(for: world.watch, selectedURL: world.chosen, baseline: nil)
        let intent = try environment.relinkSource(candidate: candidate, fallback: fallback)

        let core = RefusingCore()
        let reconciler = environment.originalRelinkReconciler(transport: core)
        let pass = await reconciler.reconcilePending()
        XCTAssertEqual(pass, [.refused(operationKey: intent.operationKey, code: "conflict")])
        let again = await reconciler.reconcilePending()
        XCTAssertEqual(again, [])
        XCTAssertEqual(core.sent, 1)
        XCTAssertEqual(environment.documentAccessRoutes().map(\.state), [.relinking])
        let restarted = try launch(world.stateRoot, TestClock(), codec: LoggingCodec())
        XCTAssertEqual(restarted.accessStore.pendingOriginalRelinks().map(\.refusalCode), ["conflict"])
        XCTAssertEqual(restarted.documentAccessRoutes().map(\.state), [.relinking])
        XCTAssertFalse(restarted.accessStore.hasUnresolvedRelink(accessRef: world.watch.accessRef))
        let resume = try await resumeOutcome(restarted, world.watch)
        XCTAssertEqual(resume as? NativeAccessError, .relinkPending)

        let replacement = try restarted.stageRelink(for: world.watch, selectedURL: world.chosen, baseline: nil)
        let next = try restarted.relinkSource(candidate: replacement, fallback: fallback)
        XCTAssertEqual(next.generation, intent.generation + 1)
        XCTAssertNotEqual(next.operationKey, intent.operationKey)
        XCTAssertEqual(restarted.accessStore.pendingOriginalRelinks(), [next])
    }

    // MARK: - Disconnect and Locate

    /// A Disconnect names no generation, so one that overtook a relink of the same route
    /// would tombstone the old generation and the relink would then reconnect what the
    /// person revoked. The revocation is immediate; Core hears the relink first, and the
    /// candidate is never activated over it.
    func testADisconnectDuringALocateRevokesAtOnceAndReachesCoreAfterTheRelink() async throws {
        let world = try await makeWorld()
        let environment = world.environment
        let candidate = try environment.stageRelink(for: world.watch, selectedURL: world.chosen, baseline: nil)
        let relink = try environment.relinkSource(candidate: candidate, fallback: fallback)

        let disconnects = try await environment.disconnectSource(
            linkedDocumentID: world.watch.linkedDocumentID, fallback: fallback
        )
        let disconnect = try XCTUnwrap(disconnects.first)
        XCTAssertEqual(disconnect.generation, relink.generation, "the generation that was revoked")
        XCTAssertEqual(environment.documentAccessRoutes().map(\.state), [.disconnected])

        let log = OperationLog()
        let relinkCore = CommittingCore(log: log)
        relinkCore.setOnline(false)
        let held = await environment.originalDisconnectReconciler(
            transport: DisconnectingCore(log: log)
        ).reconcilePending()
        XCTAssertEqual(held, [.owed(operationKey: disconnect.operationKey)], "the relink is still owed first")
        XCTAssertEqual(log.all, [])

        relinkCore.setOnline(true)
        let relinked = await environment.originalRelinkReconciler(transport: relinkCore).reconcilePending()
        XCTAssertEqual(relinked, [.acknowledged(operationKey: relink.operationKey)])
        let sent = await environment.originalDisconnectReconciler(
            transport: DisconnectingCore(log: log)
        ).reconcilePending()
        XCTAssertEqual(sent, [.acknowledged(operationKey: disconnect.operationKey)])
        XCTAssertEqual(log.all, ["relink", "disconnect"])

        // The revocation outranked the confirmation: no grant was activated.
        let route = try XCTUnwrap(environment.documentAccessRoutes().first)
        XCTAssertEqual(route.state, .disconnected)
        XCTAssertEqual(environment.accessStore.pendingOriginalRelinks(), [])
        XCTAssertFalse(world.codec.resolved.contains("chosen.txt"))
        let restarted = try launch(world.stateRoot, TestClock(), codec: LoggingCodec())
        XCTAssertEqual(restarted.documentAccessRoutes(), [route])
    }

    // MARK: - Staging and the comparison

    func testTheComparisonIsExactAndStatesWhenThereIsNothingToCompareWith() async throws {
        let world = try await makeWorld()
        let environment = world.environment
        let same = world.root.appendingPathComponent("same.txt")
        try Data("baseline".utf8).write(to: same)
        let baseline = OriginalRelinkBaseline(
            sourceDigest: "sha256:2a6a7bab0e1b47b0a9cfb2ee8f3b2e0c0f8d6e0e2f3d8b4c1b6e0c1c2e5f6a7b",
            sourceLengthBytes: 8
        )
        let differs = try environment.stageRelink(for: world.watch, selectedURL: same, baseline: baseline)
        XCTAssertEqual(differs.comparison, .differs, "a wrong digest is a difference")

        let exact = OriginalRelinkBaseline(
            sourceDigest: "sha256:" + sha256(Data("baseline".utf8)),
            sourceLengthBytes: 8
        )
        XCTAssertEqual(
            try environment.stageRelink(for: world.watch, selectedURL: same, baseline: exact).comparison,
            .identical
        )
        XCTAssertEqual(
            try environment.stageRelink(
                for: world.watch,
                selectedURL: same,
                baseline: OriginalRelinkBaseline(sourceDigest: exact.sourceDigest, sourceLengthBytes: 9)
            ).comparison,
            .differs,
            "the digest alone is not the comparison"
        )
        XCTAssertEqual(
            try environment.stageRelink(for: world.watch, selectedURL: same, baseline: nil).comparison,
            .noCapturedVersion
        )
        // Staging binds, reserves and publishes nothing.
        XCTAssertEqual(environment.accessStore.pendingOriginalRelinks(), [])
        XCTAssertEqual(environment.documentAccessRoutes().map(\.state), [.connected])
        XCTAssertEqual(environment.documentAccessRoutes().map(\.generation), [world.watch.bindingGeneration])
        XCTAssertEqual(world.codec.resolved.filter { $0 == "same.txt" }, [])
    }

    func testStagingRefusesWhatALocateCannotMove() async throws {
        let world = try await makeWorld()
        let environment = world.environment
        // A watch fixes its content kind, so a PDF cannot become the original of a text watch.
        let wrongKind = world.root.appendingPathComponent("report.pdf")
        try Data("%PDF".utf8).write(to: wrongKind)
        XCTAssertThrowsError(
            try environment.stageRelink(for: world.watch, selectedURL: wrongKind, baseline: nil)
        ) { XCTAssertEqual($0 as? OriginalRelinkError, .unsupportedType) }
        // A directory, and a file that is gone, are not files.
        let folder = world.root.appendingPathComponent("folder.txt", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        XCTAssertThrowsError(
            try environment.stageRelink(for: world.watch, selectedURL: folder, baseline: nil)
        ) { XCTAssertEqual($0 as? OriginalRelinkError, .unreadable) }
        XCTAssertThrowsError(
            try environment.stageRelink(
                for: world.watch,
                selectedURL: world.root.appendingPathComponent("missing.txt"),
                baseline: nil
            )
        ) { XCTAssertEqual($0 as? OriginalRelinkError, .unreadable) }
        // A route a person already disconnected cannot be relocated.
        _ = try await environment.disconnectSource(
            linkedDocumentID: world.watch.linkedDocumentID, fallback: fallback
        )
        XCTAssertThrowsError(
            try environment.stageRelink(for: world.watch, selectedURL: world.chosen, baseline: nil)
        ) { XCTAssertEqual($0 as? OriginalRelinkError, .notRelinkable) }
        XCTAssertEqual(environment.accessStore.pendingOriginalRelinks(), [])
    }

    func testOnlyAnEstablishedConnectedDirectRouteCanReserveAGeneration() async throws {
        let world = try await makeWorld()
        let store = world.environment.accessStore
        let binding = NativeRootAccessBinding(fallback)
        XCTAssertThrowsError(
            try store.reserveRelink(accessRef: "access-unknown", selectedURL: world.chosen, binding: binding)
        ) { XCTAssertEqual($0 as? NativeAccessError, .unknownAccess) }
        XCTAssertThrowsError(
            try store.reserveRelink(accessRef: world.watch.accessRef, selectedURL: world.root, binding: binding)
        ) { XCTAssertEqual($0 as? NativeAccessError, .accessDenied) }

        // A grant adopted for a candidate nothing published yet has no binding of Core's.
        let provisional = try store.adopt(
            accessRef: "access-provisional",
            linkedDocumentID: "doc-provisional",
            selectedURL: world.original,
            route: .directFile
        )
        XCTAssertEqual(provisional.lifecycle, .provisional)
        XCTAssertThrowsError(
            try store.reserveRelink(accessRef: "access-provisional", selectedURL: world.chosen, binding: binding)
        ) { XCTAssertEqual($0 as? NativeAccessError, .routeNotActive) }
        // And a relinking route is not re-adopted over its owed intent.
        _ = try store.reserveRelink(accessRef: world.watch.accessRef, selectedURL: world.chosen, binding: binding)
        XCTAssertThrowsError(
            try store.adopt(
                accessRef: world.watch.accessRef,
                linkedDocumentID: world.watch.linkedDocumentID,
                selectedURL: world.original,
                route: .directFile
            )
        ) { XCTAssertEqual($0 as? NativeAccessError, .relinkPending) }
    }

    /// A relinking route with no intent to settle could never be activated, and an intent
    /// against a connected route would activate twice. Neither is a document this build
    /// wrote, so neither is loaded.
    func testATornRelinkDocumentIsRefusedRatherThanInterpreted() async throws {
        let world = try await makeWorld()
        let candidate = try world.environment.stageRelink(
            for: world.watch, selectedURL: world.chosen, baseline: nil
        )
        try world.environment.relinkSource(candidate: candidate, fallback: fallback)
        let file = world.stateRoot
            .appendingPathComponent("native-access", isDirectory: true)
            .appendingPathComponent("native-access-v1.json")
        let original = try Data(contentsOf: file)
        var document = try XCTUnwrap(JSONSerialization.jsonObject(with: original) as? [String: Any])

        func rewrite(_ change: (inout [String: Any]) -> Void) throws {
            var copy = document
            change(&copy)
            try JSONSerialization.data(withJSONObject: copy, options: [.sortedKeys]).write(to: file)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        }
        func reloaded() -> Error? {
            do {
                _ = try launch(world.stateRoot, TestClock(), codec: LoggingCodec())
                return nil
            } catch {
                return error
            }
        }
        // Control: the untouched document loads.
        XCTAssertNil(reloaded())
        // A relinking route without its intent.
        try rewrite { $0["originalRelinks"] = [] }
        XCTAssertEqual(reloaded() as? NativeAccessError, .invalidState)
        // An intent that is not the one this generation's key derives.
        try rewrite {
            var intents = $0["originalRelinks"] as? [[String: Any]] ?? []
            intents[0]["operationKey"] = "relink-" + String(repeating: "0", count: 32)
            $0["originalRelinks"] = intents
        }
        document = try XCTUnwrap(JSONSerialization.jsonObject(with: original) as? [String: Any])
        try rewrite {
            var intents = $0["originalRelinks"] as? [[String: Any]] ?? []
            intents[0]["operationKey"] = "relink-" + String(repeating: "0", count: 32)
            $0["originalRelinks"] = intents
        }
        XCTAssertEqual(reloaded() as? NativeAccessError, .invalidState)
        // A connected route that still carries an intent.
        try rewrite {
            var records = $0["records"] as? [[String: Any]] ?? []
            records[0]["state"] = "connected"
            records[0]["bookmark"] = Data("x".utf8).base64EncodedString()
            $0["records"] = records
        }
        XCTAssertEqual(reloaded() as? NativeAccessError, .invalidState)
        try original.write(to: file)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        XCTAssertNil(reloaded())
    }

    // MARK: - The control

    /// The Files tab's Locate through the production flow: the native picker and the alert
    /// are the only replaced parts. Cancel is the safe default, Add separately hands the
    /// file to the ordinary Add path and moves nothing, and Use this file reserves.
    @MainActor
    func testTheLocateFlowReservesOnlyOnAnExplicitUseThisFile() async throws {
        let world = try await makeWorld()
        let environment = world.environment
        var prompts: [OriginalRelinkPrompt] = []
        var separately: [URL] = []
        var wakes = 0
        var pick: URL? = world.chosen
        var decision = OriginalRelinkDecision.cancel
        let flow = OriginalRelinkFlow(
            environment: environment,
            fallback: fallback,
            chooseFile: { pick },
            decide: { prompt in
                prompts.append(prompt)
                return decision
            },
            refuse: { _ in },
            addSeparately: { separately.append($0) },
            onReserved: { wakes += 1 }
        )

        pick = nil
        let none = await flow.locate(world.watch, history: nil)
        XCTAssertEqual(none, .noFileChosen)
        pick = world.chosen
        let declined = await flow.locate(world.watch, history: nil)
        XCTAssertEqual(declined, .cancelled)
        decision = .addSeparately
        let separate = await flow.locate(world.watch, history: nil)
        XCTAssertEqual(separate, .addedSeparately)
        XCTAssertEqual(separately, [world.chosen])
        XCTAssertEqual(prompts.count, 2)
        XCTAssertEqual(prompts[0].title, "Use “chosen.txt” as the original of “watched.txt”?")
        XCTAssertTrue(prompts[0].offersAddSeparately)
        XCTAssertEqual(wakes, 0)
        XCTAssertEqual(environment.accessStore.pendingOriginalRelinks(), [], "nothing was reserved")
        XCTAssertEqual(environment.documentAccessRoutes().map(\.state), [.connected])

        decision = .useThisFile
        let result = await flow.locate(world.watch, history: nil)
        XCTAssertEqual(
            result, .reserved(accessRef: world.watch.accessRef, generation: world.watch.bindingGeneration + 1)
        )
        XCTAssertEqual(wakes, 1)
        XCTAssertEqual(environment.documentAccessRoutes().map(\.state), [.relinking])
        XCTAssertEqual(environment.pendingRelinks().map(\.candidateName), ["chosen.txt"])
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.paused])

        // The same file cannot be put through twice: one relink is owed at a time.
        var refusals: [String] = []
        let strict = OriginalRelinkFlow(
            environment: environment,
            fallback: fallback,
            chooseFile: { world.chosen },
            decide: { _ in .useThisFile },
            refuse: { refusals.append($0) },
            addSeparately: { _ in },
            onReserved: { wakes += 1 }
        )
        let twice = await strict.locate(world.watch, history: nil)
        XCTAssertEqual(twice, .refused(.notRelinkable))
        XCTAssertEqual(wakes, 1)
        XCTAssertEqual(refusals, ["This file cannot be relocated right now. Nothing changed."])
    }

    func testThePromptStatesTheDesignsWordingAndNeverCallsAChangedHashUnrelated() {
        let differs = OriginalRelinkPrompt(
            documentLabel: "Notes.txt", selectedFileName: "Notes v2.txt", comparison: .differs
        )
        XCTAssertTrue(differs.comparisonText.hasPrefix("This file differs from the last captured version."))
        XCTAssertTrue(differs.comparisonText.contains("does not prove it is unrelated"))
        XCTAssertTrue(differs.informativeText.contains("not captured now"))
        XCTAssertTrue(differs.informativeText.contains("updates stay paused"))
        XCTAssertTrue(differs.offersAddSeparately)
        let identical = OriginalRelinkPrompt(
            documentLabel: "Notes.txt", selectedFileName: "Notes.txt", comparison: .identical
        )
        XCTAssertFalse(identical.offersAddSeparately)
        XCTAssertFalse(identical.comparisonText.contains("differs"))
        XCTAssertTrue(
            OriginalRelinkPrompt(
                documentLabel: "n", selectedFileName: "m", comparison: .noCapturedVersion
            ).offersAddSeparately
        )
    }

    func testTheRelinkRequestAndAnswerCrossTheCommandLineAsSpecified() throws {
        let delivery = CLICaptureDelivery(
            executable: URL(fileURLWithPath: "/bin/omnivia"),
            installationState: URL(fileURLWithPath: "/state"),
            deliveryBinding: fallback
        )
        XCTAssertEqual(
            delivery.relinkArguments(operationKey: "relink-k"),
            [
                "--require-running", "--installation-state", "/state",
                "--workspace-id", "workspace-1", "--timeout-ms", "20000",
                "document", "relink", "--input-stdin",
                "--idempotency-key", "relink-k",
                "--principal", "local-user", "--json",
            ]
        )
        func answer(_ result: [String: Any], status: Int32 = 0) -> OriginalRelinkOutcome {
            let data = try! JSONSerialization.data(withJSONObject: ["result": result])
            return CLICaptureDelivery.relinkOutcome(status: status, output: data, diagnostic: Data())
        }
        XCTAssertEqual(
            answer([
                "document_id": "d", "owner_ref": "o", "binding_generation": 3,
                "connection_state": "connected",
            ]),
            .acknowledged(documentID: "d", ownerRef: "o", bindingGeneration: 3, currentCaptureID: nil)
        )
        XCTAssertEqual(
            answer([
                "document_id": "d", "owner_ref": "o", "binding_generation": 3,
                "connection_state": "connected", "current_capture_id": "dcap-x",
            ]),
            .acknowledged(documentID: "d", ownerRef: "o", bindingGeneration: 3, currentCaptureID: "dcap-x")
        )
        // A result that does not say connected, or has no generation, acknowledges nothing.
        XCTAssertEqual(
            answer(["document_id": "d", "owner_ref": "o", "binding_generation": 3, "connection_state": "disconnected"]),
            .unknown
        )
        XCTAssertEqual(
            answer(["document_id": "d", "owner_ref": "o", "connection_state": "connected"]), .unknown
        )
        let refusal = try JSONSerialization.data(
            withJSONObject: ["error": ["code": "conflict", "message": "m"]]
        )
        XCTAssertEqual(
            CLICaptureDelivery.relinkOutcome(status: 1, output: refusal, diagnostic: Data()),
            .definiteFailure(code: "conflict")
        )
        let unsure = try JSONSerialization.data(
            withJSONObject: ["error": ["code": "deadline_exceeded", "details": ["outcome_established": false]]]
        )
        XCTAssertEqual(
            CLICaptureDelivery.relinkOutcome(status: 1, output: unsure, diagnostic: Data()), .unknown
        )
        let encoded = try OriginalRelinkWireEncoder().encode(
            OriginalRelinkIntent(
                accessRef: "access-1", linkedDocumentID: "doc-1", generation: 2,
                binding: NativeRootAccessBinding(fallback),
                operationKey: "relink-k", createdAt: "2026-10-16T00:00:00Z",
                candidateName: "n.txt", candidateBookmark: Data("/somewhere/n.txt".utf8),
                refusalCode: nil
            )
        )
        let text = String(decoding: encoded, as: UTF8.self)
        XCTAssertFalse(text.contains("/somewhere"), "a path never crosses the wire")
        XCTAssertFalse(text.contains("bookmark"))
    }

    // MARK: - Fixtures

    /// One admitted baseline watch over `watched.txt`, as the manual Add that establishes it
    /// leaves things, and a second, different file to Locate.
    private func makeWorld() async throws -> World {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("watched.txt")
        let chosen = root.appendingPathComponent("chosen.txt")
        try Data("baseline".utf8).write(to: original)
        try Data("the file Locate chose".utf8).write(to: chosen)
        let stateRoot = root.appendingPathComponent("state", isDirectory: true)
        let codec = LoggingCodec()
        let reader = RecordingReader()
        let clock = TestClock()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: stateRoot,
            bookmarkCodec: codec,
            reader: reader,
            monotonicClock: clock.reader
        )
        let baseline = try await ManualCaptureSubmission(
            environment: environment, workspaceRef: "workspace-1"
        ).submit([ManualCaptureCandidate(url: original, keepUpdated: true)])
        let digest = "sha256:" + String(repeating: "d", count: 64)
        let staging = try environment.queue.beginStage(
            itemID: baseline[0].intent.itemId, requestDigest: digest, descriptorDigest: digest
        )
        _ = try environment.queue.markAdmitted(
            itemID: baseline[0].intent.itemId,
            attemptGeneration: staging.attemptGeneration,
            captureID: "dcap-" + String(repeating: "a", count: 64)
        )
        _ = await environment.fileMonitoring.reconcileReceipts()
        let watch = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(watch.state, .active)
        return World(
            root: root, stateRoot: stateRoot, original: original, chosen: chosen,
            codec: codec, reader: reader, clock: clock, environment: environment, watch: watch
        )
    }

    private func launch(_ stateRoot: URL, _ clock: TestClock, codec: LoggingCodec) throws -> DocumentIntakeEnvironment {
        try DocumentIntakeEnvironment(
            stateRoot: stateRoot, bookmarkCodec: codec, monotonicClock: clock.reader
        )
    }

    /// A refresh item for the watch's route at a stated generation, as a check would build it.
    private func refreshItem(for watch: FileSubscription, generation: UInt64) -> CapturePreparationItem {
        CapturePreparationItem(
            access: NativeAccessExpectation(accessRef: watch.accessRef, generation: generation),
            seed: CaptureIntentSeed(
                submissionId: "submission-stale",
                itemId: "item-stale",
                targetRef: watch.targetRef,
                workspaceRef: watch.workspaceRef,
                targetBindingRevision: watch.targetBindingRevision,
                callerContextBinding: watch.callerContextBinding,
                surface: .manual,
                contentKind: watch.contentKind,
                payloadRef: "payload-stale",
                requestedSourceIdentity: watch.requestedSourceIdentity,
                operationKey: "capture-stale",
                intentKind: .refresh,
                basePrecondition: .expected(watch.currentSuccessfulCaptureID),
                processingProfile: ProcessingProfileBinding(profileRef: "document-default", revision: "1"),
                originalBinding: OriginalBindingIntent(
                    accessRecordId: watch.accessRef,
                    bindingGeneration: generation,
                    linkedDocumentId: watch.linkedDocumentID
                ),
                createdAt: "2026-10-16T00:00:00Z",
                displayName: watch.displayName
            )
        )
    }

    private func resumeOutcome(
        _ environment: DocumentIntakeEnvironment,
        _ watch: FileSubscription
    ) async throws -> Error? {
        do {
            _ = try await environment.fileMonitoring.resume(subscriptionID: watch.subscriptionID)
            return nil
        } catch {
            return error
        }
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

    private func failureAsync<T>(
        file: StaticString = #filePath,
        line: UInt = #line,
        _ body: () async throws -> T
    ) async throws -> Error {
        do {
            _ = try await body()
            XCTFail("expected a failure", file: file, line: line)
            throw CaptureDeliveryError.invalidIntent
        } catch {
            return error
        }
    }

    private func sha256(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-original-relink-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
