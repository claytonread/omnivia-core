import CryptoKit
import Foundation
import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

/// AT-19: a timeout, an expired replay or an unknown admission is shown as
/// Checking status, with no destructive removal and no new-key resubmission.
///
/// Both arms run the production `CLICaptureDelivery` into the production
/// `CaptureIntentQueue`, `CaptureDeliveryCoordinator`, `CaptureActivityStore`
/// and `FilesAndFoldersModel`. Nothing hands the transport an outcome:
///
/// * the timeout arm launches a real executable that never answers and lets
///   the delivery's own deadline fire, shortened through its injectable
///   `processDeadline` (production stays 25 s, pinned in
///   `CLICaptureDeliveryTests`);
/// * the unservable arm commits a real capture in a live `omnivia-core-service`,
///   makes Core unable to re-serve the stored answer, and replays through the
///   real `omnivia` CLI. Core's refusal is its own
///   (`outcome_established: false`), not a response written by the test.
///
/// The live arm needs a Python that can import this repository's packages:
/// `OMNIVIA_CORE_TEST_PYTHON` or `<repo>/.venv`, plus an optional
/// `OMNIVIA_CORE_TEST_PYTHONPATH` for dependencies the interpreter lacks. It
/// uses the launchers it writes, never an `omnivia` found on `PATH`.
@MainActor
final class UnknownOutcomeAcceptanceTests: NativeHandoffTestCase {
    // MARK: - Timeout · the delivery's own deadline fires

    func testTheDeliverysOwnDeadlineLeavesTheEntryUnknownAndShownAsChecking() async throws {
        let root = try temporaryDirectory()
        let binding = CaptureDeliveryBinding.companion(
            workspaceRef: "ws-a74255f1-5631-497d",
            targetRef: "core-target-4f1e2c"
        )
        // `service status` answers like a running Core for this target, so the
        // pre-send gate passes. The capture command never answers: it records
        // its pid and sleeps, which only the delivery's deadline can end.
        let status = root.appendingPathComponent("status.json")
        try Fixtures.document().write(to: status)
        let pidFile = root.appendingPathComponent("capture.pid")
        let hanging = root.appendingPathComponent("omnivia-hanging")
        try """
        #!/bin/sh
        case " $* " in
          *" status "*) cat '\(status.path)'; exit 0 ;;
          *" capture "*) echo $$ > '\(pidFile.path)'; exec /bin/sleep 120 ;;
        esac
        exit 64
        """.write(to: hanging, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: hanging.path)

        let companion = try launch(root: root, binding: binding)
        let queue = companion.environment.queue
        let payload = Data("hello".utf8)
        try queue.enqueue(intent: try makeIntent(payload: payload, binding: binding), payload: payload)
        let queued = try XCTUnwrap(queue.allEntries().first)

        var delivery = CLICaptureDelivery(
            executable: hanging,
            installationState: root.appendingPathComponent("installation-state"),
            deliveryBinding: binding
        )
        delivery.processDeadline = 1
        let started = Date()
        let outcomes = await CaptureDeliveryCoordinator(queue: queue, transport: delivery).deliverPending()
        let elapsed = Date().timeIntervalSince(started)

        // It waited for the deadline and no longer, and it ended the process.
        XCTAssertGreaterThanOrEqual(elapsed, 1)
        XCTAssertLessThan(elapsed, 10)
        let pid = try XCTUnwrap(Int32(String(contentsOf: pidFile, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)))
        var gone = false
        for _ in 0..<60 where !gone {
            gone = kill(pid, 0) != 0
            if !gone { try await Task.sleep(nanoseconds: 50_000_000) }
        }
        XCTAssertTrue(gone, "the timed-out capture process was left running")

        XCTAssertEqual(outcomes.map(\.state), [.unknownOutcome])
        let sent = try XCTUnwrap(outcomes.first)
        XCTAssertEqual(sent.intent, queued.intent)
        let key = try XCTUnwrap(sent.attempts.last?.operationKey)
        try assertCheckingWithKeyAndBytesPreserved(
            queue: queue,
            activity: companion.environment.captureActivity,
            expected: sent,
            keys: [key],
            payload: payload
        )

        // The next pass replays the same request under that same key — it
        // hangs and times out again — and still allocates nothing new.
        let again = await CaptureDeliveryCoordinator(queue: queue, transport: delivery).deliverPending()
        XCTAssertEqual(again, [])
        try assertCheckingWithKeyAndBytesPreserved(
            queue: queue,
            activity: companion.environment.captureActivity,
            expected: sent,
            keys: [key],
            payload: payload
        )
    }

    // MARK: - Expired replay · Core committed and cannot re-serve its answer

    func testAnUnservableCommittedAnswerStaysUnknownAndCheckingThroughTheRealCLI() async throws {
        let core = try LiveCore.initialise(in: self)
        try core.startService()
        let root = try temporaryDirectory()
        let binding = core.delivery.deliveryBinding
        let companion = try launch(root: root, binding: binding)
        let queue = companion.environment.queue
        let payload = Data("hello".utf8)
        try queue.enqueue(intent: try makeIntent(payload: payload, binding: binding), payload: payload)

        // Core commits; the receipt never reaches the companion.
        let lost = LostReceiptTransport(core.delivery)
        let first = await CaptureDeliveryCoordinator(queue: queue, transport: lost).deliverPending()
        XCTAssertEqual(first.map(\.state), [.unknownOutcome])
        let sent = try XCTUnwrap(lost.committed.first)
        guard case let .admitted(captureID) = sent.outcome else {
            return XCTFail("Core did not commit the first attempt: \(sent.outcome)")
        }
        let entry = try XCTUnwrap(queue.allEntries().first)
        XCTAssertEqual(entry.state, .unknownOutcome)
        XCTAssertEqual(entry.attempts.map(\.operationKey), [sent.key])
        XCTAssertEqual(try core.captureIDs(documentID: "doc-1"), [captureID])

        // Control: while the stored answer is intact the same replay is
        // admitted with the same capture, so what follows is the tamper's doing.
        let input = try DocumentCaptureWireEncoder().encode(entry: entry)
        let payloadURL = try queue.payloadURL(for: entry.intent.itemId)
        let honest = await core.delivery.deliver(input: input, payloadURL: payloadURL, operationKey: sent.key)
        XCTAssertEqual(honest, .admitted(captureID: captureID))

        // Test-only: with the service stopped, rewrite the stored answer so it
        // is no longer the canonical document its digest covers. The claim
        // stands; only Core's ability to re-serve it is gone.
        core.stopService()
        try core.makeStoredAnswerUnservable()
        try core.startService()

        // What Core itself says to the replay, read straight from the CLI.
        let raw = try core.runCapture(input: input, payloadURL: payloadURL, operationKey: sent.key)
        let error = try XCTUnwrap(raw["error"] as? [String: Any], "\(raw)")
        XCTAssertNil(raw["result"])
        XCTAssertEqual((error["details"] as? [String: Any])?["outcome_established"] as? Bool, false, "\(raw)")

        // The companion restarts over the same durable root and replays through
        // the production coordinator, twice.
        let restarted = try launch(root: root, binding: binding)
        let rq = restarted.environment.queue
        let recorder = RecordingTransport(core.delivery)
        for _ in 0..<2 {
            // A replay that stays unknown settles nothing, so nothing is returned.
            let pass = await CaptureDeliveryCoordinator(queue: rq, transport: recorder).deliverPending()
            XCTAssertEqual(pass, [])
        }
        XCTAssertEqual(recorder.sent.map(\.outcome), [.unknown, .unknown])
        XCTAssertEqual(recorder.sent.map(\.key), [sent.key, sent.key])

        let after = try XCTUnwrap(rq.allEntries().first)
        XCTAssertEqual(after.attemptGeneration, entry.attemptGeneration)
        XCTAssertEqual(after.intent, entry.intent)
        try assertCheckingWithKeyAndBytesPreserved(
            queue: rq,
            activity: restarted.environment.captureActivity,
            expected: entry,
            keys: [sent.key],
            payload: payload
        )

        // Core still holds exactly the one canonical capture, and still serves.
        XCTAssertEqual(try core.captureIDs(documentID: "doc-1"), [captureID])
    }

    // MARK: - Shared assertions

    /// The entry is still the one that was sent: unknown, never settled or
    /// failed, its key, generation, intent and exact bytes intact, and the
    /// person-facing projection says Checking status without a receipt.
    private func assertCheckingWithKeyAndBytesPreserved(
        queue: CaptureIntentQueue,
        activity: CaptureActivityStore,
        expected: CaptureQueueEntry,
        keys: [String],
        payload: Data,
        file: StaticString = #filePath,
        line: UInt = #line
    ) throws {
        let entries = queue.allEntries()
        XCTAssertEqual(entries.count, 1, file: file, line: line)
        let entry = try XCTUnwrap(entries.first, file: file, line: line)
        XCTAssertEqual(entry.state, .unknownOutcome, file: file, line: line)
        XCTAssertNil(entry.admittedCaptureID, file: file, line: line)
        XCTAssertNil(entry.failureCode, file: file, line: line)
        XCTAssertEqual(entry.intent, expected.intent, file: file, line: line)
        XCTAssertEqual(entry.attemptGeneration, expected.attemptGeneration, file: file, line: line)
        XCTAssertEqual(entry.attempts.map(\.operationKey), keys, file: file, line: line)
        XCTAssertEqual(entry.attempts.last?.disposition, .unknown, file: file, line: line)
        XCTAssertEqual(
            try Data(contentsOf: try queue.payloadURL(for: entry.intent.itemId)),
            payload,
            file: file,
            line: line
        )

        let records = try activity.observe(queue: entries, runs: [])
        XCTAssertEqual(records.count, 1, file: file, line: line)
        XCTAssertFalse(try XCTUnwrap(records.first, file: file, line: line).isReceipt, file: file, line: line)
        let item = FilesAndFoldersItem.activity(
            try XCTUnwrap(
                FilesAndFoldersModel.activityGroups(records: records, runs: []).first,
                file: file,
                line: line
            )
        )
        XCTAssertEqual(item.statusLabel, "Checking status…", file: file, line: line)
        XCTAssertEqual(item.activityText, "1 checking status", file: file, line: line)
        XCTAssertEqual(item.statusFilter, .needsAttention, file: file, line: line)

        // The person's removal action, Clear completed activity, leaves it
        // alone: the record, the queue entry and its bytes all stay.
        XCTAssertEqual(try activity.clearReceipts(), 0, file: file, line: line)
        XCTAssertEqual(activity.record(itemID: entry.intent.itemId), records.first, file: file, line: line)
        XCTAssertEqual(queue.allEntries(), entries, file: file, line: line)
        XCTAssertEqual(
            try Data(contentsOf: try queue.payloadURL(for: entry.intent.itemId)),
            payload,
            file: file,
            line: line
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

/// Delegates to the real CLI delivery and records what it returned.
private final class RecordingTransport: CaptureDeliveryTransport, @unchecked Sendable {
    let real: CLICaptureDelivery
    var descriptorDigest: String { real.descriptorDigest }
    var deliveryBinding: CaptureDeliveryBinding { real.deliveryBinding }
    private let lock = NSLock()
    private var log: [(key: String, outcome: CaptureDeliveryOutcome)] = []

    init(_ real: CLICaptureDelivery) { self.real = real }

    var sent: [(key: String, outcome: CaptureDeliveryOutcome)] { lock.withLock { log } }

    func deliver(input: Data, payloadURL: URL, operationKey: String) async -> CaptureDeliveryOutcome {
        let outcome = await real.deliver(input: input, payloadURL: payloadURL, operationKey: operationKey)
        lock.withLock { log.append((operationKey, outcome)) }
        return outcome
    }
}

/// Delegates to the real CLI delivery, then drops its answer: Core has
/// committed and the companion never learns it.
private final class LostReceiptTransport: CaptureDeliveryTransport, @unchecked Sendable {
    let real: CLICaptureDelivery
    var descriptorDigest: String { real.descriptorDigest }
    var deliveryBinding: CaptureDeliveryBinding { real.deliveryBinding }
    private let lock = NSLock()
    private var delivered: [(key: String, outcome: CaptureDeliveryOutcome)] = []

    init(_ real: CLICaptureDelivery) { self.real = real }

    var committed: [(key: String, outcome: CaptureDeliveryOutcome)] { lock.withLock { delivered } }

    func deliver(input: Data, payloadURL: URL, operationKey: String) async -> CaptureDeliveryOutcome {
        let outcome = await real.deliver(input: input, payloadURL: payloadURL, operationKey: operationKey)
        lock.withLock { delivered.append((operationKey, outcome)) }
        return .unknown
    }
}

/// A real workspace, installation and `omnivia-core-service`, driven through
/// launchers over this checkout's Python packages. Only the tamper is
/// test-only; the service, the CLI and every request are the production ones.
@MainActor
final class LiveCore {
    private let directory: URL
    private let cli: URL
    private let service: URL
    private let python: URL
    private let installation: URL
    private let workspace: URL
    private let workspaceID: String
    private var running: Process?

    var delivery: CLICaptureDelivery {
        CLICaptureDelivery(
            executable: cli,
            installationState: installation,
            deliveryBinding: .installedCompanion(installationState: installation, workspaceRef: workspaceID)
        )
    }

    private init(
        directory: URL, cli: URL, service: URL, python: URL,
        installation: URL, workspace: URL, workspaceID: String
    ) {
        self.directory = directory
        self.cli = cli
        self.service = service
        self.python = python
        self.installation = installation
        self.workspace = workspace
        self.workspaceID = workspaceID
    }

    static func initialise(in test: XCTestCase) throws -> LiveCore {
        let environment = ProcessInfo.processInfo.environment
        var repository = URL(fileURLWithPath: "\(#filePath)")
        for _ in 0..<5 { repository.deleteLastPathComponent() }
        // A CI row installs the packages into its own `python3`, and a row that
        // lost them must fail rather than skip green.
        let onPath = (environment["PATH"] ?? "").split(separator: ":")
            .map { "\($0)/python3" }
            .first { FileManager.default.isExecutableFile(atPath: $0) }
        let candidates = [
            environment["OMNIVIA_CORE_TEST_PYTHON"],
            repository.appendingPathComponent(".venv/bin/python").path,
            environment["CI"] != nil ? onPath : nil,
        ]
        guard let interpreter = candidates.compactMap({ $0 })
            .first(where: { FileManager.default.isExecutableFile(atPath: $0) })
        else {
            let message = "set OMNIVIA_CORE_TEST_PYTHON to a Python that can import the omnivia packages"
            if environment["CI"] != nil { XCTFail(message) }
            throw XCTSkip(message)
        }
        let packages = [
            "src", "services/omnivia-memory/src", "packages/omnivia-core-runtime/src",
            "packages/omnivia-core-cli/src", "packages/omnivia-core-mcp/src",
            "packages/omnivia-core-client/src",
        ].map { repository.appendingPathComponent($0).path }
            + (environment["OMNIVIA_CORE_TEST_PYTHONPATH"].map { [$0] } ?? [])

        // Unix socket paths are short; the ambient TMPDIR is too deep for one.
        let directory = URL(fileURLWithPath: "/tmp/at19-\(UUID().uuidString.prefix(8))", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        test.addTeardownBlock { try? FileManager.default.removeItem(at: directory) }

        func launcher(_ name: String, _ arguments: String) throws -> URL {
            let url = directory.appendingPathComponent(name)
            try """
            #!/bin/sh
            export PYTHONPATH='\(packages.joined(separator: ":"))'
            exec '\(interpreter)' -B \(arguments)
            """.write(to: url, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
            return url
        }
        let cli = try launcher("omnivia", #"-m omnivia_core_cli.main "$@""#)
        let service = try launcher("omnivia-core-service", #"-m omnivia_core_runtime.service.main "$@""#)
        let python = try launcher("python", #""$@""#)

        let workspace = directory.appendingPathComponent("workspace")
        let installation = directory.appendingPathComponent("installation-state")
        let initialised = try run(service, ["--workspace", workspace.path, "--installation-state", installation.path, "--init"])
        let workspaceID = try XCTUnwrap(
            (initialised["workspace"] as? [String: Any])?["workspace_id"] as? String
        )
        let core = LiveCore(
            directory: directory, cli: cli, service: service, python: python,
            installation: installation, workspace: workspace, workspaceID: workspaceID
        )
        test.addTeardownBlock { @MainActor in core.stopService() }
        return core
    }

    func startService() throws {
        let process = Process()
        let output = Pipe()
        process.executableURL = service
        process.arguments = [
            "--workspace", workspace.path, "--installation-state", installation.path,
            "--endpoint", "unix://\(directory.appendingPathComponent("core.sock").path)",
        ]
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        running = process
        // The service prints one readiness document; block on its pipe.
        var seen = Data()
        while true {
            let chunk = output.fileHandleForReading.availableData
            guard !chunk.isEmpty else { throw HarnessError.serviceExited }
            seen.append(chunk)
            if let document = try? JSONSerialization.jsonObject(with: seen) as? [String: Any] {
                guard document["ready"] as? Bool == true else { throw HarnessError.serviceExited }
                return
            }
        }
    }

    func stopService() {
        guard let process = running else { return }
        process.terminate()
        process.waitUntilExit()
        running = nil
    }

    /// Test-only, with the service stopped (it holds the database exclusively):
    /// re-store the committed capture's answer as non-canonical JSON under a
    /// digest that matches it. The idempotency claim and the capture are
    /// untouched, so Core can still prove the request committed but can no
    /// longer re-serve its answer — the state Core marks `outcome_established:
    /// false`. The append-only update guard is lifted for the one statement and
    /// re-created from its recorded definition, so the schema fingerprint the
    /// service checks at startup is unchanged.
    func makeStoredAnswerUnservable() throws {
        let script = #"""
        import hashlib, json, sys
        from pathlib import Path
        from omnivia_core_runtime.storage.connection import OpenMode, open_database

        connection = open_database(Path(sys.argv[1]) / "workspace.sqlite", OpenMode.EXCLUSIVE_MAINTENANCE)
        guard = "omnivia_guard_idempotency_outcomes_update"
        (guard_sql,) = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (guard,)
        ).fetchone()
        rows = connection.execute(
            "SELECT outcome_id, outcome_json FROM omnivia_idempotency_outcomes "
            "WHERE outcome_json LIKE '%capture_id%'"
        ).fetchall()
        assert len(rows) == 1, rows
        outcome_id, stored = rows[0]
        spaced = json.dumps(json.loads(stored), indent=1)
        assert spaced != stored
        digest = "sha256:" + hashlib.sha256(spaced.encode("utf-8")).hexdigest()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"DROP TRIGGER {guard}")
        connection.execute(
            "UPDATE omnivia_idempotency_outcomes SET outcome_json = ?, outcome_digest = ? "
            "WHERE outcome_id = ?",
            (spaced, digest, outcome_id),
        )
        connection.execute(guard_sql)
        connection.execute("COMMIT")
        connection.close()
        """#
        let output = Pipe()
        let process = Process()
        process.executableURL = python
        process.arguments = ["-c", script, workspace.path]
        process.standardOutput = output
        process.standardError = output
        try process.run()
        let text = String(decoding: try output.fileHandleForReading.readToEnd() ?? Data(), as: UTF8.self)
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw HarnessError.commandFailed(text) }
    }

    /// The capture command exactly as `CLICaptureDelivery` builds it, run once
    /// with its raw JSON answer returned.
    func runCapture(input: Data, payloadURL: URL, operationKey: String) throws -> [String: Any] {
        let process = Process()
        let inputPipe = Pipe()
        let output = Pipe()
        process.executableURL = cli
        process.arguments = delivery.captureArguments(payloadURL: payloadURL, operationKey: operationKey)
        process.standardInput = inputPipe
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        try inputPipe.fileHandleForWriting.write(contentsOf: input)
        try inputPipe.fileHandleForWriting.close()
        let data = try output.fileHandleForReading.readToEnd() ?? Data()
        process.waitUntilExit()
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    /// The capture ids Core holds for a document, read through its own
    /// `document history` operation rather than by inspecting its storage.
    func captureIDs(documentID: String) throws -> [String] {
        let document = try Self.run(cli, [
            "--installation-state", installation.path, "--workspace-id", workspaceID,
            "document", "history", "--input-json", "{\"document_id\":\"\(documentID)\"}", "--json",
        ])
        let result = try XCTUnwrap(document["result"] as? [String: Any])
        return try XCTUnwrap(result["captures"] as? [[String: Any]]).compactMap { $0["capture_id"] as? String }
    }

    /// The raw JSON answer of `document disconnect`, run once exactly as
    /// `CLICaptureDelivery.disconnectArguments` builds it.
    func runDisconnect(input: Data, operationKey: String) throws -> [String: Any] {
        let process = Process()
        let inputPipe = Pipe()
        let output = Pipe()
        process.executableURL = cli
        process.arguments = delivery.disconnectArguments(operationKey: operationKey)
        process.standardInput = inputPipe
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        try inputPipe.fileHandleForWriting.write(contentsOf: input)
        try inputPipe.fileHandleForWriting.close()
        let data = try output.fileHandleForReading.readToEnd() ?? Data()
        process.waitUntilExit()
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    /// The raw JSON answer of `document relink`, run once exactly as
    /// `CLICaptureDelivery.relinkArguments` builds it.
    func runRelink(input: Data, operationKey: String) throws -> [String: Any] {
        let process = Process()
        let inputPipe = Pipe()
        let output = Pipe()
        process.executableURL = cli
        process.arguments = delivery.relinkArguments(operationKey: operationKey)
        process.standardInput = inputPipe
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        try inputPipe.fileHandleForWriting.write(contentsOf: input)
        try inputPipe.fileHandleForWriting.close()
        let data = try output.fileHandleForReading.readToEnd() ?? Data()
        process.waitUntilExit()
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    /// What the Files tab reads from Core for one document, through the production reader
    /// and the real CLI transport.
    func currentHistory(documentID: String) -> CoreDocumentHistory? {
        let reader = CoreDocumentHistoryReader(
            transport: CLICoreDocumentHistoryTransport(
                executable: cli, installationState: installation, workspaceRef: workspaceID
            )
        )
        if case let .success(history) = reader.read(documentID: documentID) { return history }
        return nil
    }

    /// The production history transport over the real CLI and this workspace.
    var historyTransport: CLICoreDocumentHistoryTransport {
        CLICoreDocumentHistoryTransport(
            executable: cli, installationState: installation, workspaceRef: workspaceID
        )
    }

    /// Test-only, with the service stopped (it holds the database exclusively): the retention
    /// decision that moves a capture to expired, made through the service's own
    /// `expire_capture` under a real workspace owner. Nothing in Core's storage is written
    /// directly.
    func expireRetention(captureID: String) throws {
        let script = #"""
        import sys
        from pathlib import Path
        from omnivia_core_runtime.service.document_intake import expire_capture, owned_workspace
        from omnivia_core_runtime.service.versions import SERVER_VERSION

        with owned_workspace(
            workspace_root=Path(sys.argv[1]),
            installation_root=Path(sys.argv[2]),
            core_version=SERVER_VERSION,
        ) as runner:
            expire_capture(runner, capture_id=sys.argv[3])
        """#
        let output = Pipe()
        let process = Process()
        process.executableURL = python
        process.arguments = ["-c", script, workspace.path, installation.path, captureID]
        process.standardOutput = output
        process.standardError = output
        try process.run()
        let text = String(decoding: try output.fileHandleForReading.readToEnd() ?? Data(), as: UTF8.self)
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw HarnessError.commandFailed(text) }
    }

    /// Core's own statement of each capture of a document, by sequence: its disposition, the
    /// reason and association it was admitted under, and whether it is the current source.
    /// Read through `document history`, not through storage.
    func captureDispositions(documentID: String) throws -> [(id: String, sequence: Int, disposition: String, reason: String, association: String, current: Bool)] {
        let document = try Self.run(cli, [
            "--installation-state", installation.path, "--workspace-id", workspaceID,
            "document", "history", "--input-json", "{\"document_id\":\"\(documentID)\"}", "--json",
        ])
        let result = try XCTUnwrap(document["result"] as? [String: Any])
        let captures = try XCTUnwrap(result["captures"] as? [[String: Any]])
        return try captures.map {
            (
                id: try XCTUnwrap($0["capture_id"] as? String),
                sequence: try XCTUnwrap($0["capture_sequence"] as? Int),
                disposition: try XCTUnwrap($0["capture_disposition"] as? String),
                reason: try XCTUnwrap($0["disposition_reason"] as? String),
                association: try XCTUnwrap($0["association_kind"] as? String),
                current: try XCTUnwrap($0["is_current_source"] as? Bool)
            )
        }.sorted { $0.sequence < $1.sequence }
    }

    /// Core's own statement of a document's original-binding state, read through its
    /// `document history` operation rather than its storage.
    func bindingState(documentID: String) throws -> String {
        let document = try Self.run(cli, [
            "--installation-state", installation.path, "--workspace-id", workspaceID,
            "document", "history", "--input-json", "{\"document_id\":\"\(documentID)\"}", "--json",
        ])
        let result = try XCTUnwrap(document["result"] as? [String: Any])
        let source = try XCTUnwrap(result["source"] as? [String: Any])
        return try XCTUnwrap(source["original_binding_state"] as? String)
    }

    /// Test-only, with the service stopped (it holds the database exclusively): what Core
    /// stored for the disconnect operation. Read-only; nothing is changed.
    func storedDisconnectRecords() throws -> (bindings: [[String]], claims: Int) {
        try storedBindingRecords(operation: "document.original.disconnect")
    }

    /// Test-only, with the service stopped: every binding Core stores, and how many
    /// idempotency claims the named operation committed.
    func storedBindingRecords(operation: String) throws -> (bindings: [[String]], claims: Int) {
        let script = #"""
        import json, sys
        from pathlib import Path
        from omnivia_core_runtime.storage.connection import OpenMode, open_database

        connection = open_database(Path(sys.argv[1]) / "workspace.sqlite", OpenMode.EXCLUSIVE_MAINTENANCE)
        bindings = connection.execute(
            "SELECT owner_ref, document_id, connection_state, binding_generation "
            "FROM omnivia_document_original_bindings ORDER BY owner_ref"
        ).fetchall()
        (claims,) = connection.execute(
            "SELECT count(*) FROM omnivia_idempotency_claims WHERE operation = ?",
            (sys.argv[2],),
        ).fetchone()
        print(json.dumps({"bindings": [[str(v) for v in row] for row in bindings], "claims": claims}))
        connection.close()
        """#
        let output = Pipe()
        let process = Process()
        process.executableURL = python
        process.arguments = ["-c", script, workspace.path, operation]
        process.standardOutput = output
        process.standardError = output
        try process.run()
        let data = try output.fileHandleForReading.readToEnd() ?? Data()
        process.waitUntilExit()
        guard process.terminationStatus == 0,
              let document = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let bindings = document["bindings"] as? [[String]],
              let claims = document["claims"] as? Int
        else { throw HarnessError.commandFailed(String(decoding: data, as: UTF8.self)) }
        return (bindings, claims)
    }

    private enum HarnessError: Error { case serviceExited, commandFailed(String) }

    private static func run(_ executable: URL, _ arguments: [String]) throws -> [String: Any] {
        let process = Process()
        let output = Pipe()
        process.executableURL = executable
        process.arguments = arguments
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        let data = try output.fileHandleForReading.readToEnd() ?? Data()
        process.waitUntilExit()
        guard process.terminationStatus == 0,
              let document = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { throw HarnessError.commandFailed(arguments.joined(separator: " ")) }
        return document
    }
}
