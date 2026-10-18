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
        XCTAssertEqual(receipt.detail, "Queued on this Mac.")

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

    /// AT-11 against the target the design names: installed, initialised and
    /// eligible, with its service deliberately stopped. The companion's delivery
    /// pass runs the real CLI, which would start a stopped service for a
    /// capture, so the stop must be read first and honoured. Nothing here is a
    /// classification handed in: the stop is a real `service stop`, and the
    /// proof that nothing started is a real `service status` afterwards.
    func testAnAddToAStoppedInstalledCoreIsQueuedAndNeverStartsIt() async throws {
        let root = try temporaryDirectory()
        let cli = try realCLI(root)
        let core = try installStoppedCore(cli)
        let binding = CaptureDeliveryBinding.installedCompanion(
            installationState: core.state,
            workspaceRef: core.workspaceID
        )
        let companion = try launch(root: root, withPanel: true, binding: binding)
        let panel = try XCTUnwrap(companion.panel)
        panel.addIncoming([try writeOriginal(root, name: "invoice.txt", text: "invoice")])
        await panel.beginSubmit()?.value

        let receipt = try XCTUnwrap(panel.receipts.last)
        XCTAssertEqual(receipt.outcome, .queued(queued: 1))
        XCTAssertEqual(receipt.detail, "Queued on this Mac.")

        let transport = CLICaptureDelivery(
            executable: cli,
            installationState: core.state,
            deliveryBinding: binding
        )
        let frozen = try XCTUnwrap(companion.environment.queue.allEntries().first).intent
        let outcomes = await CaptureDeliveryCoordinator(queue: companion.environment.queue, transport: transport)
            .deliverPending()

        // Not sent, not Added, not redirected, and the service was left alone.
        XCTAssertEqual(outcomes.map(\.state), [.pending])
        XCTAssertEqual(try core.status(cli).code, "status_not_running")

        // After the companion restarts the same bytes and frozen intent wait,
        // and a second pass still leaves the stopped service stopped.
        let restarted = try launch(root: root, binding: binding)
        let queue = restarted.environment.queue
        let waiting = try XCTUnwrap(queue.allEntries().first)
        XCTAssertEqual(queue.allEntries().count, 1)
        XCTAssertEqual(waiting.state, .pending)
        XCTAssertNil(waiting.admittedCaptureID)
        XCTAssertEqual(waiting.intent, frozen)
        XCTAssertEqual(waiting.attempts.map(\.disposition), [.knownNoEffect])
        XCTAssertEqual(
            try Data(contentsOf: try queue.payloadURL(for: waiting.intent.itemId)),
            Data("invoice".utf8)
        )
        let again = await CaptureDeliveryCoordinator(queue: queue, transport: transport).deliverPending()
        XCTAssertEqual(again.map(\.state), [.pending])
        XCTAssertEqual(try core.status(cli).code, "status_not_running")
        let records = try restarted.environment.captureActivity.observe(queue: queue.allEntries(), runs: [])
        let item = FilesAndFoldersItem.activity(
            try XCTUnwrap(FilesAndFoldersModel.activityGroups(records: records, runs: []).first)
        )
        XCTAssertEqual(item.statusLabel, "Queued on this Mac")

        // Start Core is the person's own act. Once it has run, the same
        // frozen item is sent, and nothing was lost or rewritten meanwhile.
        XCTAssertEqual(try core.run(cli, "start").code, "start_started")
        XCTAssertEqual(try core.status(cli).code, "status_running")
        let sent = await CaptureDeliveryCoordinator(queue: queue, transport: transport).deliverPending()
        XCTAssertEqual(sent.map(\.state), [.admitted], "failure: \(sent.first?.failureCode ?? "none")")
        XCTAssertEqual(try XCTUnwrap(queue.allEntries().first).intent, frozen)
    }

    /// The status gate is a read, so Core can still stop after it. The wrapper
    /// below stops the real running Core at the moment the production capture
    /// command is launched — after the gate passed, before the capture connects.
    /// The capture must attach-only: nothing is sent and Core is left stopped.
    func testACoreStoppedAfterTheStatusReadIsNotRestartedByTheCapture() async throws {
        let root = try temporaryDirectory()
        let cli = try realCLI(root)
        let core = try installStoppedCore(cli)
        XCTAssertEqual(try core.run(cli, "start").code, "start_started")
        let racing = root.appendingPathComponent("omnivia-racing")
        try """
        #!/bin/sh
        case " $* " in
          *" capture "*)
            '\(cli.path)' --installation-state '\(core.state.path)' --workspace-id '\(core.workspaceID)' \
              service stop --json >/dev/null ;;
        esac
        exec '\(cli.path)' "$@"
        """.write(to: racing, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: racing.path)
        let binding = CaptureDeliveryBinding.installedCompanion(
            installationState: core.state,
            workspaceRef: core.workspaceID
        )
        let queue = try CaptureIntentQueue(root: root.appendingPathComponent("queue"))
        let payload = Data("hello".utf8)
        let intent = try makeIntent(payload: payload, binding: binding)
        try queue.enqueue(intent: intent, payload: payload)

        let outcomes = await CaptureDeliveryCoordinator(
            queue: queue,
            transport: CLICaptureDelivery(
                executable: racing,
                installationState: core.state,
                deliveryBinding: binding
            )
        ).deliverPending()

        XCTAssertEqual(outcomes.map(\.state), [.pending])
        XCTAssertEqual(queue.allEntries().first?.attempts.map(\.disposition), [.knownNoEffect])
        XCTAssertEqual(try core.status(cli).code, "status_not_running")
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
    /// credential and version refusals are definite and put the item on hold —
    /// not failed, not Checking status, and not plain queued.
    func testPreSendRefusalsAreDefiniteAndAnythingElseStaysUnknown() {
        func outcome(_ status: Int32, _ diagnostic: String, output: String = "") -> CaptureDeliveryOutcome {
            CLICaptureDelivery.outcome(
                status: status,
                output: Data(output.utf8),
                diagnostic: Data((diagnostic + "\n").utf8)
            )
        }
        XCTAssertEqual(outcome(1, CLICaptureDelivery.managedStartFailed), .notSent)
        XCTAssertEqual(outcome(1, CLICaptureDelivery.notRunning), .notSent)
        XCTAssertEqual(outcome(0, CLICaptureDelivery.notRunning), .unknown)
        XCTAssertEqual(
            outcome(3, CLICaptureDelivery.credentialRefused),
            .held(.needsAccess)
        )
        XCTAssertEqual(
            outcome(4, CLICaptureDelivery.versionDisagreed),
            .held(.updateRequired)
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

    /// A service answer that says access or the version is the problem holds the
    /// item too, unless the runtime says it could not establish the outcome.
    func testServiceAnswersAboutAccessAndVersionHoldAndAnUnestablishedOutcomeStaysUnknown() {
        func answer(_ code: String, details: String = "") -> CaptureDeliveryOutcome {
            CLICaptureDelivery.outcome(
                from: try! XCTUnwrap(JSONSerialization.jsonObject(
                    with: Data(#"{"error":{"code":"\#(code)"\#(details)}}"#.utf8)
                ) as? [String: Any]),
                terminationStatus: 3
            )
        }
        for code in ["authentication_required", "authorization_denied", "workspace_not_granted",
                     "capability_not_granted"] {
            XCTAssertEqual(answer(code), .held(.needsAccess), code)
        }
        for code in ["incompatible_version", "unsupported_minor_version", "upgrade_required"] {
            XCTAssertEqual(answer(code), .held(.updateRequired), code)
        }
        // AT-19: an outcome the runtime could not establish is never a hold.
        XCTAssertEqual(
            answer("authorization_denied", details: #","details":{"outcome_established":false}"#),
            .unknown
        )
        // Every other refusal is still a terminal definite failure.
        XCTAssertEqual(answer("invalid_request"), .definiteFailure(code: "invalid_request"))
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
            CLICaptureDelivery.notRunning,
            CLICaptureDelivery.credentialRefused,
            CLICaptureDelivery.versionDisagreed,
            CLICaptureDelivery.refusedBeforeSend,
        ] {
            XCTAssertTrue(source.contains("\"\(sentence)\""), sentence)
        }
    }

    /// A revoked or incompatible target through the coordinator: the item is held
    /// — pending, never failed — with its bytes, its history and its key. It is
    /// sent nowhere else, not sent again by any later pass or restart, and shows
    /// the fixed wording.
    func testARevokedOrIncompatibleTargetHoldsTheItemInsteadOfFailingIt() async throws {
        for (status, diagnostic, pause, label) in [
            (Int32(3), CLICaptureDelivery.credentialRefused, CaptureQueuePause.needsAccess, "Needs access"),
            (Int32(4), CLICaptureDelivery.versionDisagreed, CaptureQueuePause.updateRequired, "Update required"),
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
            XCTAssertEqual(refused, .held(pause))
            let transport = PresentTargetTransport(refused, binding: .companion(workspaceRef: "workspace-1"))

            let outcomes = await CaptureDeliveryCoordinator(queue: queue, transport: transport)
                .deliverPending()

            XCTAssertEqual(outcomes.map(\.state), [.pending], label)
            XCTAssertEqual(outcomes.first?.pause, pause)
            XCTAssertNil(outcomes.first?.failureCode)
            let key = CaptureAttemptRecord.childOperationKey(parent: "capture-1", generation: 1)
            let restarted = try CaptureIntentQueue(root: root)
            for entry in [try XCTUnwrap(queue.allEntries().first), try XCTUnwrap(restarted.allEntries().first)] {
                XCTAssertEqual(entry.state, .pending)
                XCTAssertEqual(entry.pause, pause)
                XCTAssertEqual(entry.attemptGeneration, 1)
                XCTAssertEqual(entry.attempts.map(\.disposition), [.knownNoEffect])
                XCTAssertEqual(entry.attempts.map(\.operationKey), [key])
                XCTAssertEqual(entry.intent, intent, "the frozen intent is never rewritten")
            }
            XCTAssertEqual(try Data(contentsOf: try restarted.payloadURL(for: intent.itemId)), payload)

            // No later pass — in this process or after a restart — sends it,
            // starts an attempt or allocates a key.
            let again = await CaptureDeliveryCoordinator(queue: restarted, transport: transport)
                .deliverPending()
            XCTAssertEqual(again, [])
            XCTAssertEqual(transport.sentCount, 1)
            XCTAssertEqual(restarted.allEntries().first?.attemptGeneration, 1)

            // The activity says the fixed words and no reason code.
            let records = try CaptureActivityStore(stateDirectory: root.appendingPathComponent("activity"))
                .observe(queue: restarted.allEntries(), runs: [])
            let group = try XCTUnwrap(FilesAndFoldersModel.activityGroups(records: records, runs: []).first)
            let item = FilesAndFoldersItem.activity(group)
            XCTAssertEqual(item.statusLabel, label)
            XCTAssertEqual(item.statusFilter, .needsAttention)
            XCTAssertNil(records.first?.failureCode)
            XCTAssertEqual(records.first?.fact.needsAttention, true)
            XCTAssertEqual(records.first?.isReceipt, false, "held work is never a clearable receipt")
            XCTAssertEqual(records.first?.resolvedAt, nil)
            let rawCodes = ["target_credential_refused", "target_incompatible", "needs_access", "update_required"]
            for text in [item.statusLabel, item.activityText] {
                XCTAssertFalse(rawCodes.contains { text.contains($0) }, text)
            }
        }
    }

    /// The same items, a different target: a present target with another binding
    /// is never handed held work, and nothing is retargeted.
    func testHeldWorkIsNotHandedToAnotherTargetAndResumesOnlyOnRelease() async throws {
        let root = try temporaryDirectory()
        let queue = try CaptureIntentQueue(root: root)
        let payload = Data("hello".utf8)
        let binding = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")
        let intent = try makeIntent(payload: payload, binding: binding)
        try queue.enqueue(intent: intent, payload: payload)
        let revoked = PresentTargetTransport(.held(.needsAccess), binding: binding)
        _ = await CaptureDeliveryCoordinator(queue: queue, transport: revoked).deliverPending()

        // The target now accepts the credential, but nothing releases the hold.
        let accepting = PresentTargetTransport(
            .admitted(captureID: "dcap-" + String(repeating: "a", count: 64)),
            binding: binding
        )
        let delivery = CaptureDeliveryCoordinator(queue: queue, transport: accepting)
        let idle = await delivery.deliverPending()
        XCTAssertEqual(idle, [])
        XCTAssertEqual(accepting.sentCount, 0)

        // Releasing the other reason releases nothing.
        let wrong = try await delivery.releaseHeld(.updateRequired)
        XCTAssertEqual(wrong, [])
        XCTAssertEqual(queue.allEntries().first?.pause, .needsAccess)

        // The owner releases it: the next pass sends under the next attempt's
        // own key, never the refused attempt's.
        let released = try await delivery.releaseHeld(.needsAccess)
        XCTAssertEqual(released.map(\.state), [.pending])
        XCTAssertNil(released.first?.pause)
        let sent = await delivery.deliverPending()
        XCTAssertEqual(sent.map(\.state), [.admitted])
        XCTAssertEqual(accepting.sentCount, 1)
        XCTAssertEqual(
            accepting.keys,
            [CaptureAttemptRecord.childOperationKey(parent: "capture-1", generation: 2)]
        )
        XCTAssertEqual(sent.first?.attempts.map(\.disposition), [.knownNoEffect, .admitted])
    }

    /// Missing or not-started stays known-no-effect: back to plain pending, with
    /// no hold, so a later pass retries it under a fresh attempt key.
    func testAnAbsentTargetStillReturnsToPendingWithNoHold() async throws {
        let root = try temporaryDirectory()
        let queue = try CaptureIntentQueue(root: root)
        let payload = Data("hello".utf8)
        let intent = try makeIntent(payload: payload, binding: .companion(workspaceRef: "workspace-1"))
        try queue.enqueue(intent: intent, payload: payload)
        let gone = PresentTargetTransport(.notSent, binding: .companion(workspaceRef: "workspace-1"))
        _ = await CaptureDeliveryCoordinator(queue: queue, transport: gone).deliverPending()

        let entry = try XCTUnwrap(queue.allEntries().first)
        XCTAssertEqual(entry.state, .pending)
        XCTAssertNil(entry.pause)
        XCTAssertEqual(entry.attempts.map(\.disposition), [.knownNoEffect])
    }

    // MARK: - AT-12 / AT-13 · a revoked or incompatible target, against a real Core

    /// A revoked authorisation as the real service reports it. The companion
    /// claims the principal the intent froze; the real service authenticates a
    /// different one and refuses (`authorization_denied`), which the real CLI
    /// prints as the service's own answer. Nothing here is a classification
    /// handed in. The original intent is held, never failed, resent or moved to
    /// the principal that is authenticated now.
    func testARevokedPrincipalIsRefusedByTheRealServiceAndHoldsTheOriginalIntent() async throws {
        let root = try temporaryDirectory()
        let cli = try realCLI(root)
        let core = try installStoppedCore(cli)
        XCTAssertEqual(try core.run(cli, "start").code, "start_started")
        let captures = try countingCLI(cli, in: root)
        let authorised = CaptureDeliveryBinding.installedCompanion(
            installationState: core.state,
            workspaceRef: core.workspaceID
        )
        let former = Self.principal("former-user", of: authorised)
        let payload = Data("hello".utf8)

        // The control: the same real path, the principal the service
        // authenticates, is admitted. The refusal below is the principal alone.
        let control = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("control", isDirectory: true)
        )
        try control.queue.enqueue(intent: try makeIntent(payload: payload, binding: authorised), payload: payload)
        let admitted = await CaptureDeliveryCoordinator(
            queue: control.queue,
            transport: CLICaptureDelivery(
                executable: cli, installationState: core.state, deliveryBinding: authorised
            )
        ).deliverPending()
        XCTAssertEqual(admitted.map(\.state), [.admitted], "failure: \(admitted.first?.failureCode ?? "none")")

        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true)
        )
        let intent = try makeIntent(payload: payload, binding: former)
        try environment.queue.enqueue(intent: intent, payload: payload)
        let delivery = CaptureDeliveryCoordinator(
            queue: environment.queue,
            transport: CLICaptureDelivery(
                executable: captures.launcher, installationState: core.state, deliveryBinding: former
            )
        )

        let outcomes = await delivery.deliverPending()

        XCTAssertEqual(outcomes.map(\.state), [.pending])
        XCTAssertEqual(outcomes.first?.pause, .needsAccess)
        XCTAssertNil(outcomes.first?.failureCode)
        let launches = captures.launches()
        XCTAssertEqual(launches.count, 1)
        XCTAssertTrue(launches[0].contains("--principal former-user"), launches[0])
        let held = try XCTUnwrap(environment.queue.allEntries().first)
        XCTAssertEqual(held.intent, intent, "the frozen intent is never rewritten")
        XCTAssertEqual(held.attempts.map(\.disposition), [.knownNoEffect])
        XCTAssertEqual(try Data(contentsOf: try environment.queue.payloadURL(for: intent.itemId)), payload)

        // Held means no later pass sends it, in this process or after a restart.
        let restarted = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true)
        )
        let idle = await CaptureDeliveryCoordinator(
            queue: restarted.queue,
            transport: CLICaptureDelivery(
                executable: captures.launcher, installationState: core.state, deliveryBinding: former
            )
        ).deliverPending()
        XCTAssertEqual(idle, [])
        XCTAssertEqual(captures.launches().count, 1)
        XCTAssertEqual(restarted.queue.allEntries().first?.pause, .needsAccess)
        let records = try restarted.recordCaptureActivity()
        let group = try XCTUnwrap(FilesAndFoldersModel.activityGroups(records: records, runs: []).first)
        XCTAssertEqual(FilesAndFoldersItem.activity(group).statusLabel, "Needs access")

        // The person says it is resolved, but the service still authenticates
        // someone else: the item is released, tried once, and held again. It is
        // never sent under the principal that is authenticated now.
        _ = try await delivery.releaseHeld(.needsAccess)
        let again = await delivery.deliverPending()
        XCTAssertEqual(again.map(\.state), [.pending])
        XCTAssertEqual(again.first?.pause, .needsAccess)
        XCTAssertEqual(again.first?.attempts.map(\.disposition), [.knownNoEffect, .knownNoEffect])
        XCTAssertEqual(again.first?.intent, intent)
        XCTAssertEqual(captures.launches().count, 2)
    }

    /// AT-13 during recovery, against the real service. An attempt whose outcome
    /// is unknown is replayed only under its own key, and the replay claims the
    /// principal the intent froze, so a replay under a changed account is
    /// refused by the service rather than admitted: the entry stays unknown with
    /// the same attempt and key, and nothing is retargeted.
    func testARecoveryReplayUnderAChangedPrincipalIsRefusedAndStaysUnknown() async throws {
        let root = try temporaryDirectory()
        let cli = try realCLI(root)
        let core = try installStoppedCore(cli)
        XCTAssertEqual(try core.run(cli, "start").code, "start_started")
        let captures = try countingCLI(cli, in: root)
        let authorised = CaptureDeliveryBinding.installedCompanion(
            installationState: core.state,
            workspaceRef: core.workspaceID
        )
        let former = Self.principal("former-user", of: authorised)
        let payload = Data("hello".utf8)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true)
        )
        let intent = try makeIntent(payload: payload, binding: former)
        try environment.queue.enqueue(intent: intent, payload: payload)
        let real = CLICaptureDelivery(
            executable: captures.launcher, installationState: core.state, deliveryBinding: former
        )
        // The first attempt is lost without an answer, so the entry is a real
        // recovery obligation with the request digest the coordinator froze.
        let lost = FirstAnswerLostTransport(real)
        let uncertain = await CaptureDeliveryCoordinator(queue: environment.queue, transport: lost)
            .deliverPending()
        XCTAssertEqual(uncertain.map(\.state), [.unknownOutcome])
        XCTAssertEqual(captures.launches().count, 0)
        let before = try XCTUnwrap(environment.queue.allEntries().first)

        let outcomes = await CaptureDeliveryCoordinator(queue: environment.queue, transport: lost)
            .deliverPending()

        XCTAssertEqual(outcomes, [])
        XCTAssertEqual(captures.launches().count, 1, "the replay really reached the service")
        let after = try XCTUnwrap(environment.queue.allEntries().first)
        XCTAssertEqual(after.state, .unknownOutcome)
        XCTAssertNil(after.admittedCaptureID)
        XCTAssertEqual(after.attempts, before.attempts, "no attempt, key or digest was rewritten")
        XCTAssertEqual(after.intent, intent)
        XCTAssertTrue(captures.launches().allSatisfy { $0.contains("--principal former-user") })
    }

    /// A version disagreement as the real service publishes it: a service that
    /// now speaks only a newer API than this client. The real `service status`
    /// reports it incompatible before any capture is launched; the real capture
    /// command, run directly, refuses with the CLI's fixed sentence and exit 4;
    /// and the item is held as Update required. Restoring the compatible
    /// descriptor changes nothing until the person chooses Try again, which is
    /// the production route that releases the hold and lets the next pass send.
    func testARealVersionDisagreementHoldsTheItemUntilTheWindowsTryAgain() async throws {
        let root = try temporaryDirectory()
        let cli = try realCLI(root)
        let core = try installStoppedCore(cli)
        XCTAssertEqual(try core.run(cli, "start").code, "start_started")
        let captures = try countingCLI(cli, in: root)
        let binding = CaptureDeliveryBinding.installedCompanion(
            installationState: core.state,
            workspaceRef: core.workspaceID
        )
        let descriptor = try core.descriptorURL()
        let compatible = try Data(contentsOf: descriptor)
        var newer = try XCTUnwrap(JSONSerialization.jsonObject(with: compatible) as? [String: Any])
        newer["supported_api_versions"] = ["minimum": "2.0", "maximum": "2.0"]
        let incompatible = try JSONSerialization.data(withJSONObject: newer, options: [.sortedKeys])
        func publish(_ data: Data) throws {
            try data.write(to: descriptor, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: descriptor.path)
        }
        // Teardown stops the service, which it cannot do under a descriptor for a
        // newer API, so a failed assertion below must not leave one behind.
        addTeardownBlock { try? publish(compatible) }
        try publish(incompatible)
        XCTAssertEqual(try core.status(cli).code, "status_incompatible")

        // The CLI's own refusal, from a real process.
        let direct = try Self.runCapture(
            CLICaptureDelivery(executable: cli, installationState: core.state, deliveryBinding: binding),
            input: Data(#"{"media_type":"text/plain"}"#.utf8),
            payload: root.appendingPathComponent("direct.txt"),
            key: "direct-1"
        )
        XCTAssertEqual(direct.status, 4)
        XCTAssertEqual(direct.outcome, .held(.updateRequired))

        let payload = Data("hello".utf8)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true)
        )
        let intent = try makeIntent(payload: payload, binding: binding)
        try environment.queue.enqueue(intent: intent, payload: payload)
        let delivery = CaptureDeliveryCoordinator(
            queue: environment.queue,
            transport: CLICaptureDelivery(
                executable: captures.launcher, installationState: core.state, deliveryBinding: binding
            ),
            fileSubscriptions: environment.fileSubscriptions,
            folderSubscriptions: environment.folderSubscriptions,
            gate: environment.publicationGate
        )
        let outcomes = await delivery.deliverPending()

        XCTAssertEqual(outcomes.map(\.state), [.pending])
        XCTAssertEqual(outcomes.first?.pause, .updateRequired)
        XCTAssertEqual(captures.launches().count, 0, "the status gate held it before any capture ran")
        let window = try managementWindow(environment, delivery: delivery)
        window.reload()
        let item = try XCTUnwrap(window.model.items(segment: .recentAdditions).first)
        XCTAssertEqual(item.statusLabel, "Update required")
        XCTAssertTrue(FilesAndFoldersModel.actions(for: item).tryAgainEnabled)

        // The update is installed. Nothing sends by itself.
        try publish(compatible)
        XCTAssertEqual(try core.status(cli).code, "status_running")
        let idle = await delivery.deliverPending()
        XCTAssertEqual(idle, [])
        XCTAssertEqual(captures.launches().count, 0)

        // The person's own Try again is the production release route.
        let released = await window.applyTryAgain(item)
        XCTAssertTrue(released)
        let sent = try XCTUnwrap(environment.queue.allEntries().first)
        XCTAssertEqual(sent.state, .admitted, "failure: \(sent.failureCode ?? "none")")
        XCTAssertNil(sent.pause)
        XCTAssertEqual(sent.intent, intent)
        XCTAssertEqual(sent.attempts.map(\.disposition), [.knownNoEffect, .admitted])
        XCTAssertEqual(captures.launches().count, 1)
        XCTAssertEqual(
            sent.attempts.last?.operationKey,
            CaptureAttemptRecord.childOperationKey(parent: intent.operationKey, generation: 2)
        )
    }

    /// Try again is offered for what is held and for nothing else, and choosing
    /// it while the refusal still stands holds the same item again.
    func testTryAgainReleasesOnlyHeldWorkAndAStandingRefusalHoldsItAgain() async throws {
        let root = try temporaryDirectory()
        let environment = try DocumentIntakeEnvironment(stateRoot: root)
        let payload = Data("hello".utf8)
        let binding = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")
        let intent = try makeIntent(payload: payload, binding: binding)
        try environment.queue.enqueue(intent: intent, payload: payload)
        let transport = SwitchableTransport(.held(.needsAccess), binding: binding)
        let delivery = CaptureDeliveryCoordinator(queue: environment.queue, transport: transport)
        let window = try managementWindow(environment, delivery: delivery)
        window.reload()
        let queued = try XCTUnwrap(window.model.items(segment: .recentAdditions).first)
        XCTAssertFalse(FilesAndFoldersModel.actions(for: queued).tryAgainEnabled, "nothing is held yet")
        let notHeld = await window.applyTryAgain(queued)
        XCTAssertFalse(notHeld)
        XCTAssertEqual(transport.sentCount, 0)

        _ = await delivery.deliverPending()
        window.reload()
        let held = try XCTUnwrap(window.model.items(segment: .recentAdditions).first)
        XCTAssertEqual(held.statusLabel, "Needs access")
        XCTAssertTrue(FilesAndFoldersModel.actions(for: held).tryAgainEnabled)
        let detail = window.model.detail(for: held).map(\.value).joined(separator: "\n")
        XCTAssertTrue(detail.contains("Access to this workspace is required"), detail)

        let stillRefused = await window.applyTryAgain(held)
        XCTAssertTrue(stillRefused)
        XCTAssertEqual(transport.sentCount, 2)
        let again = try XCTUnwrap(environment.queue.allEntries().first)
        XCTAssertEqual(again.pause, .needsAccess)
        XCTAssertEqual(again.attempts.map(\.disposition), [.knownNoEffect, .knownNoEffect])

        transport.outcome = .admitted(captureID: "dcap-" + String(repeating: "a", count: 64))
        window.reload()
        let retry = try XCTUnwrap(window.model.items(segment: .recentAdditions).first)
        let released = await window.applyTryAgain(retry)
        XCTAssertTrue(released)
        XCTAssertEqual(environment.queue.allEntries().first?.state, .admitted)
        XCTAssertEqual(transport.sentCount, 3)
    }

    /// AT-13 for a cached Share, end to end on the production path: a request
    /// the Share extension published while no companion ran is adopted into
    /// review, added under the account it was added under, and queued with that
    /// account frozen. The account then changes, both in this process and after
    /// a restart. The frozen intent pauses with its bytes and key; no account
    /// that is signed in later is handed it.
    func testACachedShareAddedUnderOneAccountIsNeverSentUnderAnother() async throws {
        let root = try temporaryDirectory()
        try publishShare(try shareInbox(root), [("shared.txt", "shared")])
        let former = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")
        let companion = try launch(root: root, withPanel: true, binding: former)
        XCTAssertEqual(companion.shareAdopter.reconcile().adopted.count, 1)
        await companion.panel?.beginSubmit()?.value
        let queued = try XCTUnwrap(companion.environment.queue.allEntries().first)
        XCTAssertEqual(queued.intent.surface, .share)
        XCTAssertEqual(queued.intent.callerContextBinding, former.callerContextBinding)

        let current = Self.principal("current-user", of: former)
        let signedIn = PresentTargetTransport(
            .admitted(captureID: "dcap-" + String(repeating: "a", count: 64)),
            binding: current
        )
        let delivery = CaptureDeliveryCoordinator(
            queue: companion.environment.queue,
            transport: signedIn,
            fileSubscriptions: companion.environment.fileSubscriptions,
            folderSubscriptions: companion.environment.folderSubscriptions,
            gate: companion.environment.publicationGate
        )
        let pause = try await delivery.pauseWorkUnderChangedIdentity()
        XCTAssertEqual(pause.pausedItemIDs, [queued.intent.itemId])
        let outcomes = await delivery.deliverPending()
        XCTAssertEqual(outcomes, [])
        XCTAssertEqual(signedIn.sentCount, 0)

        // The companion restarts under the new account: same item, same frozen
        // intent, same bytes, still paused and still sent to nobody.
        let restarted = try launch(root: root, binding: current)
        let waiting = try XCTUnwrap(restarted.environment.queue.allEntries().first)
        XCTAssertEqual(waiting.intent, queued.intent)
        XCTAssertEqual(waiting.state, .pending)
        XCTAssertTrue(waiting.attempts.isEmpty)
        XCTAssertEqual(
            try Data(contentsOf: try restarted.environment.queue.payloadURL(for: waiting.intent.itemId)),
            Data("shared".utf8)
        )
        let again = await CaptureDeliveryCoordinator(queue: restarted.environment.queue, transport: signedIn)
            .deliverPending()
        XCTAssertEqual(again, [])
        XCTAssertEqual(signedIn.sentCount, 0)
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
        export PATH='\(URL(fileURLWithPath: python).deletingLastPathComponent().path)':"$PATH"
        exec '\(python)' -B -m omnivia_core_cli.main "$@"
        """.write(to: launcher, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: launcher.path)
        return launcher
    }

    /// A real, initialised installation whose service has been started and then
    /// deliberately stopped, through the CLI's own `service` commands.
    private struct InstalledCore {
        let state: URL
        let workspaceID: String

        func run(_ cli: URL, _ action: String) throws -> (status: Int32, code: String) {
            let process = Process()
            let output = Pipe()
            process.executableURL = cli
            process.arguments = [
                "--installation-state", state.path, "--workspace-id", workspaceID,
                "service", action, "--json",
            ]
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            try process.run()
            let data = try output.fileHandleForReading.readToEnd() ?? Data()
            process.waitUntilExit()
            let document = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
            return (process.terminationStatus, try XCTUnwrap(document["code"] as? String))
        }

        func status(_ cli: URL) throws -> (status: Int32, code: String) { try run(cli, "status") }

        /// The descriptor this installation's service published.
        func descriptorURL() throws -> URL {
            state.appendingPathComponent("runtime/\(workspaceID)/service.json")
        }
    }

    private func installStoppedCore(_ cli: URL) throws -> InstalledCore {
        let repository = repositoryRoot()
        let python = ProcessInfo.processInfo.environment["OMNIVIA_CORE_TEST_PYTHON"]
            ?? repository.appendingPathComponent(".venv/bin/python").path
        let adjacent = URL(fileURLWithPath: python).deletingLastPathComponent()
            .appendingPathComponent("omnivia-core-service")
        let path = ([adjacent] + (ProcessInfo.processInfo.environment["PATH"] ?? "")
            .split(separator: ":")
            .map { URL(fileURLWithPath: String($0)).appendingPathComponent("omnivia-core-service") }
        ).first { FileManager.default.isExecutableFile(atPath: $0.path) }
        guard let service = path else { throw XCTSkip("omnivia-core-service must be on PATH") }
        // Unix socket paths are short; the ambient TMPDIR is too deep for one.
        let home = URL(fileURLWithPath: "/tmp/at11-\(UUID().uuidString.prefix(8))", isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        let state = home.appendingPathComponent("installation-state")
        let init_ = Process()
        let output = Pipe()
        init_.executableURL = service
        init_.arguments = [
            "--workspace", home.appendingPathComponent("workspace").path,
            "--installation-state", state.path, "--init",
        ]
        init_.standardOutput = output
        init_.standardError = FileHandle.nullDevice
        try init_.run()
        let data = try output.fileHandleForReading.readToEnd() ?? Data()
        init_.waitUntilExit()
        let document = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let core = InstalledCore(
            state: state,
            workspaceID: try XCTUnwrap((document["workspace"] as? [String: Any])?["workspace_id"] as? String)
        )
        addTeardownBlock {
            _ = try? core.run(cli, "stop")
            try? FileManager.default.removeItem(at: home)
        }
        XCTAssertEqual(try core.run(cli, "start").status, 0)
        XCTAssertEqual(try core.run(cli, "stop").code, "stop_stopped")
        XCTAssertEqual(try core.status(cli).code, "status_not_running")
        return core
    }

    private static func principal(_ ref: String, of binding: CaptureDeliveryBinding) -> CaptureDeliveryBinding {
        CaptureDeliveryBinding(
            targetRef: binding.targetRef,
            workspaceRef: binding.workspaceRef,
            targetBindingRevision: binding.targetBindingRevision,
            callerContextBinding: CallerContextBinding(
                clientContextRef: binding.callerContextBinding.clientContextRef,
                principalRef: ref,
                contextRevision: binding.callerContextBinding.contextRevision
            )
        )
    }

    /// The real CLI behind a launcher that records every capture it is asked to
    /// run, so a test can say how many times anything was sent at all.
    private func countingCLI(_ cli: URL, in root: URL) throws -> (launcher: URL, launches: () -> [String]) {
        let log = root.appendingPathComponent("captures.log")
        let launcher = root.appendingPathComponent("omnivia-counting")
        try """
        #!/bin/sh
        case " $* " in
          *" capture "*) echo "$*" >> '\(log.path)' ;;
        esac
        exec '\(cli.path)' "$@"
        """.write(to: launcher, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: launcher.path)
        return (launcher, {
            ((try? String(contentsOf: log, encoding: .utf8)) ?? "")
                .split(separator: "\n").map(String.init)
        })
    }

    /// One production capture command run as a real process, answered by the
    /// production classifier.
    private static func runCapture(
        _ transport: CLICaptureDelivery,
        input: Data,
        payload: URL,
        key: String
    ) throws -> (status: Int32, outcome: CaptureDeliveryOutcome) {
        try Data("hello".utf8).write(to: payload)
        let process = Process()
        let inputPipe = Pipe(), outputPipe = Pipe(), errorPipe = Pipe()
        process.executableURL = transport.executable
        process.arguments = transport.captureArguments(payloadURL: payload, operationKey: key)
        process.standardInput = inputPipe
        process.standardOutput = outputPipe
        process.standardError = errorPipe
        try process.run()
        try inputPipe.fileHandleForWriting.write(contentsOf: input)
        try inputPipe.fileHandleForWriting.close()
        let output = try outputPipe.fileHandleForReading.readToEnd() ?? Data()
        let diagnostic = try errorPipe.fileHandleForReading.readToEnd() ?? Data()
        process.waitUntilExit()
        return (
            process.terminationStatus,
            CLICaptureDelivery.outcome(status: process.terminationStatus, output: output, diagnostic: diagnostic)
        )
    }

    /// The window as the companion builds it: Resume and Check now ask the
    /// delivery coordinator about the identity, and Try again releases the holds
    /// and runs the next delivery pass.
    private func managementWindow(
        _ environment: DocumentIntakeEnvironment,
        delivery: CaptureDeliveryCoordinator
    ) throws -> FolderManagementWindowController {
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
            identityAuthorises: { await delivery.confirmsIdentity(of: $0) },
            releaseHeld: { pauses in
                for pause in pauses.sorted(by: { $0.rawValue < $1.rawValue }) {
                    _ = try? await delivery.releaseHeld(pause)
                }
                _ = await delivery.deliverPending()
            }
        )
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
    private var sentKeys: [String] = []

    init(_ outcome: CaptureDeliveryOutcome, binding: CaptureDeliveryBinding) {
        self.outcome = outcome
        deliveryBinding = binding
    }

    var sentCount: Int { lock.withLock { sent } }
    var keys: [String] { lock.withLock { sentKeys } }

    func deliver(input: Data, payloadURL: URL, operationKey: String) async -> CaptureDeliveryOutcome {
        lock.withLock {
            sent += 1
            sentKeys.append(operationKey)
        }
        return outcome
    }
}

/// A target whose answer a test can change between passes.
private final class SwitchableTransport: @unchecked Sendable, CaptureDeliveryTransport {
    let descriptorDigest = "sha256:" + String(repeating: "b", count: 64)
    let deliveryBinding: CaptureDeliveryBinding
    private let lock = NSLock()
    private var current: CaptureDeliveryOutcome
    private var sent = 0

    init(_ outcome: CaptureDeliveryOutcome, binding: CaptureDeliveryBinding) {
        current = outcome
        deliveryBinding = binding
    }

    var outcome: CaptureDeliveryOutcome {
        get { lock.withLock { current } }
        set { lock.withLock { current = newValue } }
    }

    var sentCount: Int { lock.withLock { sent } }

    func deliver(input: Data, payloadURL: URL, operationKey: String) async -> CaptureDeliveryOutcome {
        lock.withLock { sent += 1 }
        return outcome
    }
}

/// Loses the first answer and then delegates, so the first attempt of an intent
/// is a genuine unknown outcome whose replay the real transport must answer.
private final class FirstAnswerLostTransport: @unchecked Sendable, CaptureDeliveryTransport {
    private let real: CLICaptureDelivery
    private let lock = NSLock()
    private var lostFirst = false

    init(_ real: CLICaptureDelivery) { self.real = real }

    var descriptorDigest: String { real.descriptorDigest }
    var deliveryBinding: CaptureDeliveryBinding { real.deliveryBinding }

    func deliver(input: Data, payloadURL: URL, operationKey: String) async -> CaptureDeliveryOutcome {
        let first = lock.withLock { () -> Bool in
            defer { lostFirst = true }
            return !lostFirst
        }
        if first { return .unknown }
        return await real.deliver(input: input, payloadURL: payloadURL, operationKey: operationKey)
    }
}
