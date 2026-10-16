import CryptoKit
import Foundation
import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

/// AT-11 and AT-12 against a target that is really not there.
///
/// The delivery client is the production `CLICaptureDelivery`. An absent
/// executable is a real failed launch. An absent installation is the real
/// `omnivia` CLI run against a state root that does not exist, so the refusal
/// is the CLI's own, not a transport that was told what to answer. Those tests
/// need a Python that can import the packages and skip, naming the variable,
/// when none is configured: `OMNIVIA_CORE_TEST_PYTHON` or `<repo>/.venv`.
@MainActor
final class AbsentTargetDeliveryAcceptanceTests: NativeHandoffTestCase {
    // MARK: - AT-11 · offline, the Add is queued and stays queued

    /// The whole offline Add on the production path: the panel's own receipt
    /// says the design's sentence, and a delivery pass against a Core this Mac
    /// cannot reach neither loses nor misreports the item.
    func testAnOfflineAddIsQueuedWithTheDesignSentenceAndStaysQueuedAfterAFailedPass() async throws {
        let root = try temporaryDirectory()
        let companion = try launch(root: root, withPanel: true)
        let panel = try XCTUnwrap(companion.panel)
        panel.addIncoming([try writeOriginal(root, name: "invoice.txt", text: "invoice")])
        await panel.beginSubmit()?.value

        let receipt = try XCTUnwrap(panel.receipts.last)
        XCTAssertEqual(receipt.outcome, .queued(queued: 1))
        XCTAssertEqual(receipt.detail, "Queued on this Mac. Open OmniVia Core to send.")

        let transport = CLICaptureDelivery(
            executable: try realCLI(root),
            installationState: root.appendingPathComponent("no-such-installation"),
            deliveryBinding: .companion(workspaceRef: "workspace-1")
        )
        let queue = companion.environment.queue
        let outcomes = await CaptureDeliveryCoordinator(queue: queue, transport: transport)
            .deliverPending()

        // Nothing was sent, so the item is exactly what the Add published.
        XCTAssertEqual(outcomes.map(\.state), [.pending])
        let entry = try XCTUnwrap(queue.allEntries().first)
        XCTAssertEqual(entry.state, .pending)
        XCTAssertNil(entry.admittedCaptureID)
        XCTAssertEqual(entry.attempts.map(\.disposition), [.knownNoEffect])
        XCTAssertEqual(
            try Data(contentsOf: try queue.payloadURL(for: entry.intent.itemId)),
            Data("invoice".utf8)
        )
        // And the activity the person reads still says queued, not Checking.
        let records = try companion.environment.captureActivity.observe(
            queue: queue.allEntries(),
            runs: []
        )
        let item = FilesAndFoldersItem.activity(
            try XCTUnwrap(FilesAndFoldersModel.activityGroups(records: records, runs: []).first)
        )
        XCTAssertEqual(item.statusLabel, "Queued on this Mac")
    }

    /// The Share extension is outside the Swift test target, so what it shows
    /// is pinned at the source: both of its queued states display the shared
    /// constant, and neither says added.
    func testTheShareExtensionDisplaysTheDesignSentenceForBothQueuedStates() throws {
        let source = try String(
            contentsOf: repositoryRoot().appendingPathComponent(
                "apps/core-status-menu-macos/Extensions/Share/ShareViewController.swift"
            ),
            encoding: .utf8
        )
        XCTAssertEqual(
            source.components(separatedBy: "status.stringValue = ShareInboxStore.queuedWording").count - 1,
            2
        )
        XCTAssertFalse(source.contains("status.stringValue = \"Added"))
    }

    // MARK: - AT-12 · a missing target is not a delivery

    /// The executable the target is reached through has gone. Launching fails,
    /// so nothing was sent.
    func testAnExecutableThatIsGoneIsReportedAsNotSent() async throws {
        let root = try temporaryDirectory()
        let transport = CLICaptureDelivery(
            executable: root.appendingPathComponent("omnivia-is-gone"),
            installationState: root.appendingPathComponent("state"),
            deliveryBinding: .companion(workspaceRef: "workspace-1")
        )
        let outcome = await transport.deliver(
            input: Data("{}".utf8),
            payloadURL: root.appendingPathComponent("payload"),
            operationKey: "capture-1"
        )
        XCTAssertEqual(outcome, .notSent)
    }

    /// A target whose installation is absent. The real CLI cannot start a
    /// service, the item goes back to pending with its bytes, every later pass
    /// is the same refusal rather than a stuck "Checking status", and a present
    /// target with a different binding is never handed the work.
    func testAnAbsentInstallationLeavesTheItemPendingAndNeverFallsBack() async throws {
        let root = try temporaryDirectory()
        let cli = try realCLI(root)
        let absent = CaptureDeliveryBinding.installedCompanion(
            installationState: root.appendingPathComponent("no-such-installation"),
            workspaceRef: "workspace-1"
        )
        let queue = try CaptureIntentQueue(root: root.appendingPathComponent("queue"))
        let payload = Data("hello".utf8)
        let intent = try makeIntent(payload: payload, binding: absent)
        try queue.enqueue(intent: intent, payload: payload)
        let delivery = CaptureDeliveryCoordinator(
            queue: queue,
            transport: CLICaptureDelivery(
                executable: cli,
                installationState: root.appendingPathComponent("no-such-installation"),
                deliveryBinding: absent
            )
        )

        let first = await delivery.deliverPending()
        let second = await delivery.deliverPending()

        XCTAssertEqual(first.map(\.state), [.pending])
        XCTAssertEqual(second.map(\.state), [.pending])
        let entry = try XCTUnwrap(queue.allEntries().first)
        XCTAssertEqual(entry.state, .pending)
        XCTAssertEqual(entry.attemptGeneration, 2)
        XCTAssertEqual(entry.attempts.map(\.disposition), [.knownNoEffect, .knownNoEffect])
        XCTAssertEqual(
            Set(entry.attempts.map(\.operationKey)).count,
            2,
            "a replacement attempt never reuses the key of the one before"
        )
        XCTAssertEqual(entry.intent, intent, "the frozen intent is never rewritten")
        XCTAssertEqual(try Data(contentsOf: try queue.payloadURL(for: intent.itemId)), payload)

        // A different target that is present — and would admit — is not a
        // fallback: it is not the binding this work was frozen against.
        let present = PresentTargetTransport(
            .admitted(captureID: "dcap-" + String(repeating: "a", count: 64)),
            binding: .companion(workspaceRef: "workspace-1")
        )
        let refused = await CaptureDeliveryCoordinator(queue: queue, transport: present)
            .deliverPending()
        XCTAssertEqual(refused, [])
        XCTAssertEqual(present.sentCount, 0)
        XCTAssertEqual(queue.allEntries().first?.state, .pending)
    }

    /// The same bytes survive a restart of the queue, before any target
    /// returns.
    func testTheQueuedBytesSurviveARestartWhileTheTargetIsAbsent() async throws {
        let root = try temporaryDirectory()
        let queueRoot = root.appendingPathComponent("queue")
        let queue = try CaptureIntentQueue(root: queueRoot)
        let payload = Data("hello".utf8)
        let intent = try makeIntent(payload: payload, binding: .companion(workspaceRef: "workspace-1"))
        try queue.enqueue(intent: intent, payload: payload)
        let gone = CLICaptureDelivery(
            executable: root.appendingPathComponent("omnivia-is-gone"),
            installationState: root.appendingPathComponent("state"),
            deliveryBinding: .companion(workspaceRef: "workspace-1")
        )
        _ = await CaptureDeliveryCoordinator(queue: queue, transport: gone).deliverPending()

        let restarted = try CaptureIntentQueue(root: queueRoot)

        XCTAssertEqual(restarted.allEntries().map(\.state), [.pending])
        XCTAssertEqual(
            try Data(contentsOf: try restarted.payloadURL(for: intent.itemId)),
            payload
        )
    }

    // MARK: - AT-12 · revoked and incompatible targets are definite refusals

    /// The CLI's pre-send refusals, read as the companion reads them. The
    /// credential and version refusals are definite and are shown as failures
    /// with their reason — not as Checking status, and not as queued.
    func testPreSendRefusalsAreDefiniteAndAnythingElseStaysUnknown() {
        func outcome(_ status: Int32, _ diagnostic: String, output: String = "") -> CaptureDeliveryOutcome {
            CLICaptureDelivery.outcome(
                status: status,
                output: Data(output.utf8),
                diagnostic: Data((diagnostic + "\n").utf8)
            )
        }
        XCTAssertEqual(outcome(1, CLICaptureDelivery.managedStartFailed), .notSent)
        XCTAssertEqual(
            outcome(3, CLICaptureDelivery.credentialRefused),
            .definiteFailure(code: "target_credential_refused")
        )
        XCTAssertEqual(
            outcome(4, CLICaptureDelivery.versionDisagreed),
            .definiteFailure(code: "target_incompatible")
        )
        XCTAssertEqual(
            outcome(2, CLICaptureDelivery.refusedBeforeSend),
            .definiteFailure(code: "refused_before_send")
        )
        // The same exit status with another sentence, or the sentence with
        // another status, could follow a send: unknown.
        XCTAssertEqual(outcome(1, "the call did not complete"), .unknown)
        XCTAssertEqual(outcome(1, CLICaptureDelivery.credentialRefused), .unknown)
        XCTAssertEqual(outcome(0, CLICaptureDelivery.managedStartFailed), .unknown)
        XCTAssertEqual(outcome(6, "the call ran out of time, or was cancelled"), .unknown)
        XCTAssertEqual(outcome(1, ""), .unknown)
        // A service answer outranks anything on stderr.
        XCTAssertEqual(
            outcome(
                7,
                CLICaptureDelivery.managedStartFailed,
                output: #"{"error":{"code":"dependency_unavailable"}}"#
            ),
            .definiteFailure(code: "dependency_unavailable")
        )
    }

    /// The sentences are the CLI's, byte for byte; a reworded CLI would
    /// otherwise turn these refusals back into unknown outcomes unnoticed.
    func testTheDiagnosticsAreTheCLIsOwnFixedSentences() throws {
        let source = try String(
            contentsOf: repositoryRoot().appendingPathComponent(
                "packages/omnivia-core-cli/src/omnivia_core_cli/main.py"
            ),
            encoding: .utf8
        )
        for sentence in [
            CLICaptureDelivery.managedStartFailed,
            CLICaptureDelivery.credentialRefused,
            CLICaptureDelivery.versionDisagreed,
            CLICaptureDelivery.refusedBeforeSend,
        ] {
            XCTAssertTrue(source.contains("\"\(sentence)\""), sentence)
        }
    }

    /// A revoked or incompatible target through the coordinator: a failed
    /// item with the reason, delivered nowhere else, its recovery copy kept
    /// across a restart.
    func testARevokedTargetFailsTheItemWithItsReasonAndKeepsTheRecoveryCopy() async throws {
        for (status, diagnostic, code) in [
            (Int32(3), CLICaptureDelivery.credentialRefused, "target_credential_refused"),
            (Int32(4), CLICaptureDelivery.versionDisagreed, "target_incompatible"),
        ] {
            let root = try temporaryDirectory()
            let queue = try CaptureIntentQueue(root: root)
            let payload = Data("hello".utf8)
            let intent = try makeIntent(payload: payload, binding: .companion(workspaceRef: "workspace-1"))
            try queue.enqueue(intent: intent, payload: payload)
            let refused = CLICaptureDelivery.outcome(
                status: status,
                output: Data(),
                diagnostic: Data((diagnostic + "\n").utf8)
            )
            let transport = PresentTargetTransport(refused, binding: .companion(workspaceRef: "workspace-1"))

            let outcomes = await CaptureDeliveryCoordinator(queue: queue, transport: transport)
                .deliverPending()

            XCTAssertEqual(outcomes.map(\.state), [.failed], code)
            XCTAssertEqual(outcomes.first?.failureCode, code)
            let restarted = try CaptureIntentQueue(root: root)
            XCTAssertEqual(restarted.allEntries().first?.state, .failed)
            // The queue stops resolving a failed item's bytes, but the last
            // recovery copy is still on disk, whole.
            let retained = try XCTUnwrap(
                FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil)?
                    .compactMap { $0 as? URL }
                    .first { $0.lastPathComponent == "payload-1.payload" },
                "the failed item keeps its recovery copy"
            )
            XCTAssertEqual(try Data(contentsOf: retained), payload)
            // A later pass does not resend it anywhere.
            let again = await CaptureDeliveryCoordinator(queue: restarted, transport: transport)
                .deliverPending()
            XCTAssertEqual(again, [])
            XCTAssertEqual(transport.sentCount, 1)
        }
    }

    // MARK: - Helpers

    private func repositoryRoot(_ file: StaticString = #filePath) -> URL {
        var url = URL(fileURLWithPath: "\(file)")
        for _ in 0..<5 { url.deleteLastPathComponent() }
        return url
    }

    /// A launcher around the real `omnivia` CLI module — the production code,
    /// not a stand-in — or a skip when this host cannot run it.
    private func realCLI(_ root: URL) throws -> URL {
        let repository = repositoryRoot()
        let python = ProcessInfo.processInfo.environment["OMNIVIA_CORE_TEST_PYTHON"]
            ?? repository.appendingPathComponent(".venv/bin/python").path
        try XCTSkipUnless(
            FileManager.default.isExecutableFile(atPath: python),
            "set OMNIVIA_CORE_TEST_PYTHON to a Python that can import the omnivia packages"
        )
        let path = [
            "src",
            "services/omnivia-memory/src",
            "packages/omnivia-core-runtime/src",
            "packages/omnivia-core-cli/src",
            "packages/omnivia-core-mcp/src",
            "packages/omnivia-core-client/src",
        ].map { repository.appendingPathComponent($0).path }.joined(separator: ":")
        let launcher = root.appendingPathComponent("omnivia")
        try """
        #!/bin/sh
        export PYTHONPATH='\(path)'
        exec '\(python)' -B -m omnivia_core_cli.main "$@"
        """.write(to: launcher, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: launcher.path)
        return launcher
    }

    private func makeIntent(payload: Data, binding: CaptureDeliveryBinding) throws -> CaptureIntentV3 {
        let digest = "sha256:" + SHA256.hash(data: payload).map { String(format: "%02x", $0) }.joined()
        return try CaptureIntentV3(
            submissionId: "submission-1",
            itemId: "item-1",
            targetRef: binding.targetRef,
            workspaceRef: binding.workspaceRef,
            targetBindingRevision: binding.targetBindingRevision,
            callerContextBinding: binding.callerContextBinding,
            surface: .manual,
            contentKind: .text,
            payloadRef: "payload-1",
            byteLength: UInt64(payload.count),
            localIntegrityDigest: digest,
            requestedSourceIdentity: "source-1",
            operationKey: "capture-1",
            intentKind: .newCapture,
            basePrecondition: .create,
            processingProfile: ProcessingProfileBinding(profileRef: "document-default", revision: "1"),
            originalBinding: OriginalBindingIntent(
                accessRecordId: "access-1",
                bindingGeneration: 1,
                linkedDocumentId: "doc-1"
            ),
            createdAt: "2026-10-14T00:00:00Z",
            displayName: "note.txt"
        )
    }
}

/// A target that is present and would answer; used to show it is never handed
/// work frozen against another binding.
private final class PresentTargetTransport: @unchecked Sendable, CaptureDeliveryTransport {
    let descriptorDigest = "sha256:" + String(repeating: "b", count: 64)
    let deliveryBinding: CaptureDeliveryBinding
    private let outcome: CaptureDeliveryOutcome
    private let lock = NSLock()
    private var sent = 0

    init(_ outcome: CaptureDeliveryOutcome, binding: CaptureDeliveryBinding) {
        self.outcome = outcome
        deliveryBinding = binding
    }

    var sentCount: Int { lock.withLock { sent } }

    func deliver(input: Data, payloadURL: URL, operationKey: String) async -> CaptureDeliveryOutcome {
        lock.withLock { sent += 1 }
        return outcome
    }
}
