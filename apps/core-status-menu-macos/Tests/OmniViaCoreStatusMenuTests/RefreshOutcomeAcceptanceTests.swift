import CryptoKit
import Foundation
import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS

/// AT-32 and AT-33 on the companion's production stores, coordinator and Files window.
///
/// * AT-32: a refresh that fails after a previous success leaves that capture where it was,
///   and the failure is a record of its own — in the watch, in the delivery queue and in the
///   Recent additions record — that survives a restart and reads beside the capture it left
///   in place rather than instead of it.
/// * AT-33: the same bytes are "checked" only on Core's own statement that the capture behind
///   them is retained, authorised and retrieval-ready. The statement is read through the
///   production history reader and the real CLI transport (a stand-in `omnivia` executable
///   that returns the canned answer); the coordinator, stores and window are the real ones.
///   The same flows against a live Core are `RefreshOutcomeLiveCoreTests`.
final class RefreshOutcomeAcceptanceTests: XCTestCase {
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

    /// One delivery attempt, answered the same way every time.
    private final class FixedTransport: CaptureDeliveryTransport, @unchecked Sendable {
        let descriptorDigest = "sha256:" + String(repeating: "c", count: 64)
        let deliveryBinding = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")
        private let outcome: CaptureDeliveryOutcome
        init(_ outcome: CaptureDeliveryOutcome) { self.outcome = outcome }

        func deliver(
            input: Data,
            payloadURL: URL,
            operationKey: String
        ) async -> CaptureDeliveryOutcome { outcome }
    }

    private static let head = "dcap-" + String(repeating: "a", count: 64)
    private static let baseline = Data("baseline".utf8)

    // MARK: - AT-32 · the failed refresh is its own record

    func testAFailedRefreshIsItsOwnRecordBesideThePriorCaptureAndSurvivesARestart() async throws {
        let rig = try await failedRefresh()

        // The capture that succeeded is exactly where it was.
        let after = try XCTUnwrap(rig.environment.fileSubscriptions.all().first)
        XCTAssertEqual(after.currentSuccessfulCaptureID, Self.head)
        XCTAssertEqual(after.state, .paused)

        // The failure is a record of its own: it names the refused item, Core's reason and the
        // capture it left in place.
        let failed = try XCTUnwrap(
            rig.environment.queue.allEntries().first { $0.intent.surface == .automaticFile }
        )
        XCTAssertEqual(failed.state, .failed)
        let attention = try XCTUnwrap(after.attention)
        XCTAssertEqual(attention.kind, .refreshFailed)
        XCTAssertEqual(attention.code, "unsupported_media_type")
        XCTAssertEqual(attention.itemID, failed.intent.itemId)
        XCTAssertEqual(attention.priorCaptureID, Self.head)
        XCTAssertNotEqual(failed.intent.itemId, rig.watch.baselineItemID)

        // The same two facts, still two facts, after a restart over the same durable state.
        let restarted = try DocumentIntakeEnvironment(
            stateRoot: rig.stateRoot,
            bookmarkCodec: BookmarkCodec(),
            monotonicClock: rig.clock.reader
        )
        let reread = try XCTUnwrap(restarted.fileSubscriptions.all().first)
        XCTAssertEqual(reread.attention, attention)
        XCTAssertEqual(reread.currentSuccessfulCaptureID, Self.head)
        XCTAssertEqual(
            restarted.queue.allEntries().first { $0.intent.itemId == failed.intent.itemId }?.failureCode,
            "unsupported_media_type"
        )
        XCTAssertEqual(
            try restarted.recordCaptureActivity().first { $0.itemID == failed.intent.itemId }?.fact,
            .failed
        )
    }

    @MainActor
    func testTheFilesWindowShowsTheFailedRefreshBesideTheCaptureItLeftInPlace() async throws {
        let rig = try await failedRefresh()
        let restarted = try DocumentIntakeEnvironment(
            stateRoot: rig.stateRoot,
            bookmarkCodec: BookmarkCodec(),
            monotonicClock: rig.clock.reader
        )
        let failedItem = try XCTUnwrap(
            restarted.queue.allEntries().first { $0.intent.surface == .automaticFile }
        ).intent.itemId

        func read(_ availability: CoreHistoryAvailability) throws -> (
            row: FilesAndFoldersItem, facts: [String: String], recent: [String: String]
        ) {
            let window = Self.window(restarted)
            window.historyState = CoreHistoryState(
                documentID: rig.watch.linkedDocumentID, availability: availability
            )
            window.reload()
            let model = window.model
            let row = try XCTUnwrap(model.items(segment: .files).first)
            let recent = try XCTUnwrap(
                model.items(segment: .recentAdditions).first {
                    $0.searchableIdentifiers.contains(failedItem)
                }
            )
            return (
                row,
                Dictionary(uniqueKeysWithValues: model.detail(for: row).map { ($0.label, $0.value) }),
                Dictionary(uniqueKeysWithValues: model.detail(for: recent).map { ($0.label, $0.value) })
            )
        }

        // Core still serves the prior capture: both statements are made, side by side.
        let serving = try read(.current(try Self.history()))
        XCTAssertEqual(serving.row.statusLabel, "Refresh failed")
        XCTAssertEqual(serving.row.statusFilter, .needsAttention)
        let latest = try XCTUnwrap(serving.facts["Latest refresh"])
        XCTAssertTrue(latest.contains("Failed at"), latest)
        XCTAssertTrue(latest.contains("unsupported_media_type"), latest)
        XCTAssertTrue(latest.contains("Nothing was replaced"), latest)
        XCTAssertTrue(latest.contains("remains available and searchable in Core"), latest)
        XCTAssertTrue(latest.contains(Self.head), latest)
        XCTAssertTrue(latest.contains(failedItem), latest)
        XCTAssertTrue(
            serving.facts["Current searchable version"]?.hasPrefix("Extraction 1") ?? false,
            "\(serving.facts)"
        )
        // And the failure is its own Recent additions record, with the reason it carries.
        XCTAssertTrue(serving.recent.values.contains { $0.contains("unsupported_media_type") })

        // Core no longer holds the prior capture: the failure is not allowed to claim it does.
        let expired = try read(.current(try Self.history(retention: "expired")))
        let gone = try XCTUnwrap(expired.facts["Latest refresh"])
        XCTAssertTrue(gone.contains("no longer available"), gone)
        XCTAssertFalse(gone.contains("remains available"), gone)

        // And when Core cannot be read just now, it says so instead of guessing.
        let unknown = try XCTUnwrap(
            try read(.unavailable(.unavailable)).facts["Latest refresh"]
        )
        XCTAssertTrue(unknown.contains("could not be confirmed"), unknown)
    }

    func testARefreshWhoseProcessingFailedInCoreIsNotReadAsTheSearchableVersion() throws {
        let second = "dcap-" + String(repeating: "b", count: 64)
        let history = try Self.history(
            captures: [
                Self.captureWire(id: Self.head, sequence: 1, current: false),
                Self.captureWire(
                    id: second, sequence: 2, digest: Self.sha("changed"), current: true,
                    intent: "refresh", base: Self.head
                ),
            ],
            currentCapture: second,
            headSequence: 2,
            extractions: [
                Self.extractionWire(id: "dext-1", capture: Self.head, ordinal: 1, current: true),
                Self.extractionWire(
                    id: "dext-2", capture: second, ordinal: 2, current: false,
                    result: "failed", publication: "not_published"
                ),
            ]
        )
        let file = Self.sampleFile()
        let model = FilesAndFoldersModel(
            workspaceName: "workspace-1",
            files: [file],
            coreHistory: CoreHistoryState(
                documentID: file.linkedDocumentID, availability: .current(history)
            )
        )
        let facts = Dictionary(
            uniqueKeysWithValues: model.detail(for: .file(file)).map { ($0.label, $0.value) }
        )

        // The newest source is the refresh, and it says its own processing failed; the version
        // retrieval serves is still the earlier one, and the failure is its own extraction row.
        let capture = try XCTUnwrap(facts["Source capture 2"])
        XCTAssertTrue(capture.contains("its processing failed"), capture)
        XCTAssertTrue(capture.contains("earlier searchable version is unchanged"), capture)
        XCTAssertTrue(facts["Extraction 2"]?.contains("Processing failed") ?? false)
        XCTAssertTrue(facts["Extraction 2"]?.contains("nothing was replaced") ?? false)
        XCTAssertTrue(facts["Current searchable version"]?.hasPrefix("Extraction 1") ?? false)
        XCTAssertFalse(try XCTUnwrap(facts["Source capture 1"]).contains("processing failed"))
    }

    // MARK: - AT-33 · same bytes, evidence that no longer stands

    func testSameBytesWhoseCaptureExpiredAreNeverCheckedAndNothingIsRepublished() async throws {
        let rig = try await unchangedWatch()
        rig.cli.answer(Self.historyWire(retention: "expired"))

        let reports = try await rig.check()
        XCTAssertEqual(reports.map(\.queued), [false])

        // Not "checked": no time recorded, nothing published, the capture untouched; the
        // generation is finished (the read happened) and the shortfall is recorded as itself.
        let value = try XCTUnwrap(rig.environment.fileSubscriptions.all().first)
        XCTAssertNil(value.lastCheckedAt)
        XCTAssertEqual(value.state, .active)
        XCTAssertFalse(value.needsReconciliation)
        XCTAssertEqual(value.currentSuccessfulCaptureID, Self.head)
        XCTAssertEqual(value.attention?.kind, .copyNotCurrent)
        XCTAssertEqual(value.attention?.code, CurrentCaptureShortfall.captureNotRetained.rawValue)
        XCTAssertEqual(value.attention?.priorCaptureID, Self.head)
        XCTAssertNil(rig.environment.automaticScheduling.state(of: rig.key))
        XCTAssertTrue(
            rig.environment.queue.allEntries().allSatisfy { $0.intent.surface != .automaticFile },
            "an unchanged file is never re-added over a retention decision"
        )

        // The record is durable.
        let restarted = try rig.reopen()
        XCTAssertEqual(restarted.fileSubscriptions.all().first?.attention, value.attention)

        // The window says what is true: not up to date, and why.
        let file = try XCTUnwrap(restarted.fileSubscriptions.all().first)
        let model = FilesAndFoldersModel(workspaceName: "workspace-1", files: [file])
        let row = try XCTUnwrap(model.items(segment: .files).first)
        XCTAssertEqual(row.statusLabel, "Copy in Core not current")
        XCTAssertEqual(row.statusFilter, .needsAttention)
        let text = try XCTUnwrap(
            model.detail(for: row).first { $0.label == "Latest check" }?.value
        )
        XCTAssertTrue(text.contains("not up to date"), text)
        XCTAssertTrue(text.contains("expired or been deleted"), text)
        XCTAssertTrue(text.contains("checked time was not advanced"), text)
    }

    func testEveryShortfallOfCoresCopyBlocksTheUpToDateClaim() async throws {
        let cases: [(CurrentCaptureShortfall, String)] = [
            (.captureNotRetained, Self.historyWire(retention: "deleted")),
            (.sourceRemoved, Self.historyWire(sourceState: "removed")),
            (.originalNotConnected, Self.historyWire(binding: "disconnected")),
            (.notRetrievable, Self.historyWire(retrieval: false)),
            // Core's current source is a newer capture than the one this watch holds.
            (.captureDiffers,
                Self.historyWire(
                    captures: [
                        Self.captureWire(id: Self.head, sequence: 1, current: false),
                        Self.captureWire(
                            id: "dcap-" + String(repeating: "b", count: 64), sequence: 2,
                            digest: Self.sha("other"), current: true, intent: "refresh",
                            base: Self.head
                        ),
                    ],
                    currentCapture: "dcap-" + String(repeating: "b", count: 64),
                    headSequence: 2
                )),
            // Core's capture is not the bytes the file holds.
            (.captureDiffers, Self.historyWire(digest: Self.sha("something else"))),
        ]
        for (shortfall, wire) in cases {
            let rig = try await unchangedWatch()
            rig.cli.answer(wire)
            _ = try await rig.check()
            let value = try XCTUnwrap(rig.environment.fileSubscriptions.all().first)
            XCTAssertNil(value.lastCheckedAt, "\(shortfall)")
            XCTAssertEqual(value.attention?.code, shortfall.rawValue)
            XCTAssertTrue(
                rig.environment.queue.allEntries().allSatisfy { $0.intent.surface != .automaticFile },
                "\(shortfall)"
            )
        }
    }

    /// AT-32 meets AT-33: the refresh Core admitted is the current source, but its processing
    /// failed, so the version retrieval serves belongs to the capture before it. Unchanged bytes
    /// of the refresh are not "up to date"; nothing searchable stands behind them.
    func testUnchangedBytesOfARefreshWhoseProcessingFailedAreNotUpToDate() throws {
        let second = "dcap-" + String(repeating: "b", count: 64)
        let history = try Self.history(
            captures: [
                Self.captureWire(id: Self.head, sequence: 1, current: false),
                Self.captureWire(
                    id: second, sequence: 2, digest: Self.sha("changed"), current: true,
                    intent: "refresh", base: Self.head
                ),
            ],
            currentCapture: second,
            headSequence: 2,
            extractions: [
                Self.extractionWire(id: "dext-1", capture: Self.head, ordinal: 1, current: true),
            ]
        )
        XCTAssertEqual(
            CoreHistoryCurrentCaptureVerifier.verdict(
                history, captureID: second, observedDigest: Self.sha("changed")
            ),
            .notCurrent(.notRetrievable)
        )
    }

    func testCoreNotAnsweringEstablishesNothingAndAnAccessRefusalIsNeedsAccess() async throws {
        // No verifier at all: a local hash is not Core's statement.
        let bare = try await unchangedWatch(verifier: false)
        _ = try await bare.check()
        let unverified = try XCTUnwrap(bare.environment.fileSubscriptions.all().first)
        XCTAssertNil(unverified.lastCheckedAt)
        XCTAssertNil(unverified.attention)
        XCTAssertTrue(unverified.needsReconciliation)
        XCTAssertNotNil(bare.environment.automaticScheduling.state(of: bare.key))

        // Core answers with something this build cannot read.
        let unreadable = try await unchangedWatch()
        unreadable.cli.raw(Data("not json".utf8))
        _ = try await unreadable.check()
        let kept = try XCTUnwrap(unreadable.environment.fileSubscriptions.all().first)
        XCTAssertNil(kept.lastCheckedAt)
        XCTAssertNil(kept.attention)
        XCTAssertEqual(kept.state, .active)
        XCTAssertTrue(kept.needsReconciliation)

        // Core no longer accepts this Mac's credential.
        let denied = try await unchangedWatch()
        denied.cli.refusal("authorization_denied")
        _ = try await denied.check()
        let held = try XCTUnwrap(denied.environment.fileSubscriptions.all().first)
        XCTAssertNil(held.lastCheckedAt)
        XCTAssertEqual(held.state, .needsAccess)
        XCTAssertEqual(held.currentSuccessfulCaptureID, Self.head)
    }

    func testBytesCoreStandsBehindAreCheckedAndClearAnEarlierShortfall() async throws {
        let rig = try await unchangedWatch()
        rig.cli.answer(Self.historyWire(retention: "expired"))
        _ = try await rig.check()
        XCTAssertNotNil(rig.environment.fileSubscriptions.all().first?.attention)

        // Core's copy stands again (the person added the file again; the capture is current).
        rig.cli.answer(Self.historyWire())
        _ = try await rig.check()
        let value = try XCTUnwrap(rig.environment.fileSubscriptions.all().first)
        XCTAssertNotNil(value.lastCheckedAt)
        XCTAssertNil(value.attention)
        XCTAssertEqual(value.state, .active)
        XCTAssertTrue(
            rig.environment.queue.allEntries().allSatisfy { $0.intent.surface != .automaticFile }
        )
    }

    func testAWatchSavedBeforeTheseOutcomesExistedDecodesWithNone() throws {
        let file = Self.sampleFile()
        var encoded = try XCTUnwrap(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(file)) as? [String: Any]
        )
        XCTAssertNil(encoded["attention"], "an absent outcome is not written")
        encoded["attention"] = nil
        let decoded = try JSONDecoder().decode(
            FileSubscription.self, from: JSONSerialization.data(withJSONObject: encoded)
        )
        XCTAssertNil(decoded.attention)
        XCTAssertEqual(decoded, file)
    }

    // MARK: - Rigs

    /// A watch that only exists as a row, for the facts that need no store behind them.
    private static func sampleFile() -> FileSubscription {
        FileSubscription(
            subscriptionID: "file-1",
            accessRef: "access-file-1",
            linkedDocumentID: "document-file-1",
            requestedSourceIdentity: "identity-file-1",
            displayName: "Notes.txt",
            contentKind: .text,
            bindingGeneration: 1,
            baselineItemID: "baseline-file-1",
            targetRef: "target-1",
            workspaceRef: "workspace-1",
            targetBindingRevision: "binding-1",
            callerContextBinding: CallerContextBinding(
                clientContextRef: "client-1",
                principalRef: "principal-1",
                contextRevision: "context-1"
            ),
            configGeneration: 1,
            consentGeneration: 1,
            consentRecordedAt: "2026-10-14T00:00:00Z",
            state: .active,
            currentSuccessfulCaptureID: head,
            lastObservedDigest: sha("baseline"),
            lastObservedByteLength: 8,
            lastObservedAt: nil,
            lastQueuedItemID: nil,
            dirtyEpoch: 1,
            needsReconciliation: false,
            lastCheckedAt: nil
        )
    }

    private struct Watch {
        let subscriptionID: String
        let linkedDocumentID: String
        let baselineItemID: String
    }

    private struct FailedRefresh {
        let environment: DocumentIntakeEnvironment
        let stateRoot: URL
        let clock: TestClock
        let watch: Watch
    }

    /// A watch with an admitted baseline, a changed original and a refresh Core refuses.
    private func failedRefresh() async throws -> FailedRefresh {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("watched.txt")
        try Self.baseline.write(to: original)
        let stateRoot = root.appendingPathComponent("state", isDirectory: true)
        let clock = TestClock()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: stateRoot, bookmarkCodec: BookmarkCodec(), monotonicClock: clock.reader
        )
        let watch = try await admittedWatch(environment, original: original)
        try Data("changed".utf8).write(to: original)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [watch.subscriptionID])
        clock.advance(3_000)
        _ = await environment.fileMonitoring.reconcileAll()
        try environment.recordCaptureActivity()
        let refused = await CaptureDeliveryCoordinator(
            queue: environment.queue,
            transport: FixedTransport(.definiteFailure(code: "unsupported_media_type"))
        ).deliverPending()
        XCTAssertEqual(refused.map(\.state), [.failed])
        try environment.recordCaptureActivity()
        _ = await environment.fileMonitoring.reconcileAll()
        return FailedRefresh(
            environment: environment, stateRoot: stateRoot, clock: clock, watch: watch
        )
    }

    private func admittedWatch(
        _ environment: DocumentIntakeEnvironment,
        original: URL
    ) async throws -> Watch {
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
            captureID: Self.head
        )
        _ = await environment.fileMonitoring.reconcileReceipts()
        let value = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(value.state, .active)
        return Watch(
            subscriptionID: value.subscriptionID,
            linkedDocumentID: value.linkedDocumentID,
            baselineItemID: value.baselineItemID
        )
    }

    /// The stand-in `omnivia` executable: it drains its input and prints whatever answer the
    /// test last set, so the production transport really spawns a process and decodes its
    /// output.
    private final class FakeCLI: @unchecked Sendable {
        let executable: URL
        private let answerFile: URL
        init(directory: URL) throws {
            executable = directory.appendingPathComponent("omnivia")
            answerFile = directory.appendingPathComponent("answer.json")
            try """
            #!/bin/sh
            cat > /dev/null
            cat '\(answerFile.path)'
            """.write(to: executable, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o700], ofItemAtPath: executable.path
            )
            try Data().write(to: answerFile)
        }

        /// The document the answers are about: the fixture's placeholder is replaced by it.
        var documentID = "document-file-1"

        func answer(_ wire: String) {
            raw(RefreshOutcomeAcceptanceTests.envelope(
                result: wire.replacingOccurrences(of: "document-file-1", with: documentID)
            ))
        }

        func refusal(_ code: String) {
            raw(Data(
                """
                {"metadata": {"request_id": "req-1", "correlation_id": "cor-1",
                              "version": {"api_version": "\(supportedCoreHistoryContractVersion)"},
                              "authority": {"principal_id": "local-user"}},
                 "error": {"code": "\(code)", "message": "refused"}}
                """.utf8
            ))
        }

        func raw(_ data: Data) { try? data.write(to: answerFile) }
    }

    private struct UnchangedWatch {
        let environment: DocumentIntakeEnvironment
        let stateRoot: URL
        let clock: TestClock
        let cli: FakeCLI
        let key: AutomaticSchedulingKey

        func reopen() throws -> DocumentIntakeEnvironment {
            try DocumentIntakeEnvironment(
                stateRoot: stateRoot,
                bookmarkCodec: BookmarkCodec(),
                monotonicClock: clock.reader
            )
        }

        func check() async throws -> [FileMonitoringReport] {
            let id = try XCTUnwrap(environment.fileSubscriptions.all().first).subscriptionID
            await environment.fileMonitoring.markDirty(subscriptionIDs: [id])
            clock.advance(3_000)
            return await environment.fileMonitoring.reconcileAll()
        }
    }

    /// An admitted baseline watch whose original has not changed, checked through the
    /// production verifier over the real CLI transport.
    private func unchangedWatch(verifier: Bool = true) async throws -> UnchangedWatch {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("watched.txt")
        try Self.baseline.write(to: original)
        let stateRoot = root.appendingPathComponent("state", isDirectory: true)
        let clock = TestClock()
        let cli = try FakeCLI(directory: root)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: stateRoot,
            bookmarkCodec: BookmarkCodec(),
            monotonicClock: clock.reader,
            currentCapture: verifier
                ? CoreHistoryCurrentCaptureVerifier(
                    transport: CLICoreDocumentHistoryTransport(
                        executable: cli.executable,
                        installationState: root.appendingPathComponent("installation"),
                        workspaceRef: "workspace-1"
                    )
                )
                : nil
        )
        _ = try await admittedWatch(environment, original: original)
        let value = try XCTUnwrap(environment.fileSubscriptions.all().first)
        cli.documentID = value.linkedDocumentID
        return UnchangedWatch(
            environment: environment,
            stateRoot: stateRoot,
            clock: clock,
            cli: cli,
            key: FileMonitoringCoordinator.schedulingKey(for: value)
        )
    }

    // MARK: - Core history wire

    private static func sha(_ text: String) -> String {
        "sha256:" + SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    private static func captureWire(
        id: String,
        sequence: Int,
        digest: String = sha("baseline"),
        retention: String = "retained",
        current: Bool,
        intent: String = "new_capture",
        base: String? = nil
    ) -> String {
        let retained = retention == "retained"
        let actions = retained
            ? #"[{"action": "reprocess_captured_copy", "available": true}]"#
            : #"[{"action": "reprocess_captured_copy", "available": false, "unavailable_reason": "source_not_retained"}]"#
        let profiles = retained
            ? """
            [{"profile_ref": "document-default", "revision": "1",
              "processing_mode": "text_only", "profile_digest": "sha256:aaaa"}]
            """
            : "[]"
        return """
        {"capture_id": "\(id)", "capture_sequence": \(sequence), "evidence_id": "evd-\(sequence)",
         "source_digest": "\(digest)", "source_length_bytes": 8,
         "media_type": "text/plain", "requested_source_identity": "source-1",
         "intent_kind": "\(intent)", "association_kind": "active_binding",
         "capture_disposition": "head_eligible", "disposition_reason": "eligible",
         "is_current_source": \(current), "retention_state": "\(retention)",
         "capture_retention_generation": 1, "source_removal_generation": 1,
         "requested_profile": {"profile_ref": "document-default", "revision": "1"},
         "processing_state": "settled",
         \(base.map { "\"expected_base_capture_id\": \"\($0)\"," } ?? "")
         "captured_at": "2026-10-05T09:00:30Z",
         "retention_changed_at": "2026-10-05T09:00:30Z",
         "actions": \(actions), "reprocess_profiles": \(profiles)}
        """
    }

    private static func extractionWire(
        id: String,
        capture: String,
        ordinal: Int,
        current: Bool,
        result: String = "succeeded",
        publication: String = "promoted"
    ) -> String {
        let succeeded = result == "succeeded"
        return """
        {"extraction_revision_id": "\(id)", "capture_id": "\(capture)",
         "document_ordinal": \(ordinal), "extraction_sequence": 1,
         "profile": {"digest": "sha256:aaaa", "decoder_build": "document-default:1:plain-text-2",
                     "processing_mode": "text_only", "embedded_image_ocr": false,
                     "ocr_languages": []},
         "head_eligible": true, "is_current_extraction": \(current),
         "result_state": "\(result)", "publication_disposition": "\(publication)",
         \(succeeded ? "\"output_digest\": \"sha256:bbbb\"," : "")
         "worker_build": "core-portable-text-1", "completed_at": "2026-10-05T09:02:00Z",
         "admitted_at": "2026-10-05T09:01:00Z"}
        """
    }

    /// Core's history of the watched document, on the wire. The defaults are a retained capture
    /// of the baseline bytes with a published extraction over it; each argument takes away or
    /// changes one thing Core could report differently.
    private static func historyWire(
        retention: String = "retained",
        sourceState: String = "active",
        binding: String = "connected",
        retrieval: Bool = true,
        digest: String = sha("baseline"),
        captures: [String]? = nil,
        currentCapture: String = head,
        headSequence: Int = 1,
        extractions: [String]? = nil
    ) -> String {
        let captures = captures ?? [
            captureWire(id: head, sequence: 1, digest: digest, retention: retention, current: true),
        ]
        let extractions = extractions ?? [
            extractionWire(id: "dext-1", capture: head, ordinal: 1, current: true),
        ]
        let published = retrieval && !extractions.isEmpty
        return """
        {"document_id": "document-file-1",
         "created_at": "2026-10-05T09:00:00Z",
         "source": {"source_state": "\(sourceState)", "source_removal_generation": 1,
                    "current_capture_id": "\(currentCapture)",
                    "capture_head_sequence": \(headSequence),
                    "original_binding_state": "\(binding)",
                    "updated_at": "2026-10-05T09:02:00Z"},
         "extraction": {\(published ? #""current_extraction_id": "dext-1","# : "")
                        "extraction_head_ordinal": \(published ? 1 : 0),
                        "retrieval_state": "\(published ? "retrievable" : "none")"
                        \(published ? #", "published_at": "2026-10-05T09:02:00Z""# : "")},
         "captures": [\(captures.joined(separator: ", "))],
         "capture_count": \(captures.count),
         "extractions": [\(published ? extractions.joined(separator: ", ") : "")],
         "extraction_count": \(published ? extractions.count : 0),
         "actions": [{"action": "refresh_original", "available": true}],
         "page": {}}
        """
    }

    /// The same, decoded by the production decoder into the value the window holds.
    private static func history(
        retention: String = "retained",
        captures: [String]? = nil,
        currentCapture: String = head,
        headSequence: Int = 1,
        extractions: [String]? = nil
    ) throws -> CoreDocumentHistory {
        let wire = historyWire(
            retention: retention,
            captures: captures,
            currentCapture: currentCapture,
            headSequence: headSequence,
            extractions: extractions
        )
        let decoded: Result<CoreDocumentHistoryPage, CoreHistoryError> =
            CLICoreDocumentHistoryTransport.decode(envelope(result: wire))
        switch decoded {
        case let .success(page):
            return CoreDocumentHistory(
                documentID: page.documentID,
                createdAt: page.createdAt,
                source: page.source,
                extraction: page.extraction,
                captures: page.captures,
                captureCount: page.captureCount,
                extractions: page.extractions,
                extractionCount: page.extractionCount,
                actions: page.actions,
                complete: true
            )
        case let .failure(error):
            XCTFail("the fixture is not a readable page: \(error)")
            throw error
        }
    }

    private static func envelope(result: String) -> Data {
        Data(
            """
            {"metadata": {"request_id": "req-1", "correlation_id": "cor-1",
                          "version": {"api_version": "\(supportedCoreHistoryContractVersion)"},
                          "authority": {"principal_id": "local-user"}},
             "result": \(result)}
            """.utf8
        )
    }

    // MARK: - Window

    @MainActor
    private static func window(_ environment: DocumentIntakeEnvironment) -> FolderManagementWindowController {
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
            documentRoutes: { environment.documentAccessRoutes() }
        )
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-refresh-outcome-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
