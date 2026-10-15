// The Document processing pane's two preference rows, and the client behind
// them.
//
// Two halves, both without a service on the machine. The process is injected,
// so what is checked there is the argument vector, the bounds and the strict
// reading of the response envelope. The administering client is stubbed, so
// what is checked in the model is which state the pane is in, what it may act
// on, and what happens around a write.

import Foundation
import XCTest

@testable import CoreSettingsMacOS

// MARK: - Fixtures

private let executable = URL(fileURLWithPath: "/usr/local/bin/omnivia")
private let installationState = URL(fileURLWithPath: "/Users/someone/Library/omnivia/state")
private let workspace = "ws-local"

/// The success branch of the canonical response envelope, as `--json` prints
/// it: the whole envelope, with the api version this build reads.
private func envelope(
    mode: String = "text_and_local_ocr",
    languages: String = "[\"eng\"]",
    embedded: Bool = false,
    revision: Int = 0,
    apiVersion: String = "1.3"
) -> String {
    """
    {"metadata": {"version": {"api_version": "\(apiVersion)"}}, "result": {"settings": \
    {"embedded_image_ocr": \(embedded), "mode": "\(mode)", \
    "recognition_languages": \(languages), "revision": \(revision)}}}
    """
}

/// The error branch. An error response is an answer, and `--json` prints it.
private func refusal(_ code: String) -> String {
    """
    {"error": {"code": "\(code)", "message": "refused"}, \
    "metadata": {"version": {"api_version": "1.3"}}}
    """
}

private func settings(
    mode: String = "text_and_local_ocr",
    languages: [String] = ["eng"],
    embedded: Bool = false,
    revision: Int = 0
) -> DocumentProcessingSettings {
    DocumentProcessingSettings(
        modeCode: mode, recognitionLanguages: languages, embeddedImageOcr: embedded,
        revision: revision
    )
}

private func inventory(_ languages: [String]) -> OcrResourceInventory {
    OcrResourceInventory(
        capacityBytes: 536_870_912,
        installedBytes: 0,
        availableBytes: 536_870_912,
        resources: languages.map { language in
            OcrInstalledResource(
                identity: OcrResourceIdentity(
                    language: language,
                    engineVersion: "5.5.3",
                    modelVersion: "tessdata_fast-4.1.0",
                    byteSize: 4_113_088,
                    sha256: String(repeating: "a", count: 64)
                ),
                pinned: false,
                pinnedBy: [],
                bytesPresent: true
            )
        }
    )
}

/// Records every invocation and answers from a queue.
private final class RecordingProcessRunner: OcrProcessRunning, @unchecked Sendable {
    private(set) var invocations: [[String]] = []
    private(set) var timeouts: [TimeInterval] = []
    private var queued: [Result<OcrProcessResult, OcrProcessFailure>]

    init(_ queued: [Result<OcrProcessResult, OcrProcessFailure>]) { self.queued = queued }

    convenience init(stdout: String, code: Int32 = 0, truncated: Bool = false) {
        self.init([.success(OcrProcessResult(
            stdout: stdout, stderr: "", code: code, truncated: truncated
        ))])
    }

    func run(
        executable: URL,
        arguments: [String],
        timeout: TimeInterval,
        stdoutLimit: Int,
        stderrLimit: Int
    ) async -> Result<OcrProcessResult, OcrProcessFailure> {
        invocations.append(arguments)
        timeouts.append(timeout)
        guard !queued.isEmpty else {
            return .success(OcrProcessResult(stdout: "", stderr: "", code: 70, truncated: false))
        }
        return queued.removeFirst()
    }
}

private func runner(_ process: OcrProcessRunning) -> DocumentProcessingCommandRunner {
    guard let client = DocumentProcessingCommandRunner(
        executable: executable,
        installationState: installationState,
        workspaceRef: workspace,
        process: process
    ) else { preconditionFailure("the fixture paths are absolute") }
    return client
}

/// A scripted administering client. Each call pops the next queued answer and
/// records what was asked, so a write's follow-up read is visible.
private final class StubAdministrator: DocumentProcessingAdministering, @unchecked Sendable {
    enum Call: Equatable {
        case read
        case update(revision: Int, change: DocumentProcessingChange)
    }

    private(set) var calls: [Call] = []
    private var reads: [Result<DocumentProcessingSettings, DocumentProcessingError>]
    private var writes: [Result<DocumentProcessingSettings, DocumentProcessingError>]

    init(
        reads: [Result<DocumentProcessingSettings, DocumentProcessingError>],
        writes: [Result<DocumentProcessingSettings, DocumentProcessingError>] = []
    ) {
        self.reads = reads
        self.writes = writes
    }

    func read() async -> Result<DocumentProcessingSettings, DocumentProcessingError> {
        calls.append(.read)
        return reads.isEmpty ? .failure(.unreadableOutput) : reads.removeFirst()
    }

    func update(
        revision: Int, change: DocumentProcessingChange
    ) async -> Result<DocumentProcessingSettings, DocumentProcessingError> {
        calls.append(.update(revision: revision, change: change))
        return writes.isEmpty ? .failure(.unreadableOutput) : writes.removeFirst()
    }
}

// MARK: - The client

final class DocumentProcessingCommandRunnerTests: XCTestCase {
    func testAReadNamesTheGetOperationAndSendsAnEmptyInput() async {
        let process = RecordingProcessRunner(stdout: envelope())
        _ = await runner(process).read()

        XCTAssertEqual(process.invocations.count, 1)
        XCTAssertEqual(process.invocations[0], [
            "--installation-state", installationState.path,
            "--workspace-id", workspace,
            "--timeout-ms", "18000",
            "document", "settings",
            "--input-json", "{}",
            "--json",
        ])
        // A read carries no idempotency key: the CLI refuses one for an
        // operation that does not accept it.
        XCTAssertFalse(process.invocations[0].contains("--idempotency-key"))
    }

    func testAReadDecodesTheSettingsAndTheirRevision() async {
        let process = RecordingProcessRunner(
            stdout: envelope(
                mode: "text_only", languages: "[\"eng\", \"deu\"]", embedded: true, revision: 4
            )
        )
        let result = await runner(process).read()

        XCTAssertEqual(
            try? result.get(),
            settings(mode: "text_only", languages: ["eng", "deu"], embedded: true, revision: 4)
        )
        XCTAssertEqual((try? result.get())?.mode, .textOnly)
    }

    /// The whole point of the revision: the write names the one that was read.
    func testAProfileChangeIsSentUnderTheRevisionItWasReadAt() async {
        let process = RecordingProcessRunner(stdout: envelope(mode: "text_only", revision: 4))
        let result = await runner(process)
            .update(revision: 3, change: .mode(.textOnly))

        XCTAssertEqual(process.invocations[0], [
            "--installation-state", installationState.path,
            "--workspace-id", workspace,
            "--timeout-ms", "55000",
            "document", "configure",
            "--input-json", "{\"mode\":\"text_only\",\"revision\":3}",
            "--json",
            "--idempotency-key", "docproc-3-mode-text_only",
        ])
        XCTAssertEqual((try? result.get())?.revision, 4)
    }

    func testAnEmbeddedImageChangeSendsOnlyThatMember() async {
        let process = RecordingProcessRunner(stdout: envelope(embedded: true, revision: 2))
        _ = await runner(process).update(revision: 1, change: .embeddedImageOcr(true))

        let arguments = process.invocations[0]
        XCTAssertEqual(
            arguments[arguments.firstIndex(of: "--input-json")! + 1],
            "{\"embedded_image_ocr\":true,\"revision\":1}"
        )
        XCTAssertEqual(arguments.last, "docproc-1-embedded-on")
        // A partial update: the members the user did not touch are not sent,
        // so a preference this build does not know cannot be reset by it.
        XCTAssertFalse(arguments.contains { $0.contains("recognition_languages") })
        XCTAssertFalse(arguments.contains { $0.contains("\"mode\"") })
    }

    /// Equal keys with different inputs are an `idempotency_conflict`, so every
    /// member of the input has to be in the key.
    func testTheIdempotencyKeyIsDerivedFromTheRevisionAndTheChange() {
        let keys = [
            DocumentProcessingCommandRunner.idempotencyKey(revision: 1, change: .mode(.textOnly)),
            DocumentProcessingCommandRunner.idempotencyKey(revision: 2, change: .mode(.textOnly)),
            DocumentProcessingCommandRunner.idempotencyKey(
                revision: 1, change: .mode(.textAndLocalOcr)
            ),
            DocumentProcessingCommandRunner.idempotencyKey(
                revision: 1, change: .embeddedImageOcr(true)
            ),
            DocumentProcessingCommandRunner.idempotencyKey(
                revision: 1, change: .embeddedImageOcr(false)
            ),
        ]
        XCTAssertEqual(Set(keys).count, keys.count)
        // The contract's `IdempotencyKey` shape: a leading alphanumeric, then
        // alphanumerics and `._:-`, and at most 128 of them.
        for key in keys {
            XCTAssertLessThanOrEqual(key.count, 128)
            XCTAssertTrue(key.first?.isLetter == true || key.first?.isNumber == true)
            XCTAssertTrue(key.allSatisfy { character in
                character.isASCII
                    && (character.isLetter || character.isNumber || "._:-".contains(character))
            })
        }
    }

    /// Nothing on this surface installs a resource. The two leaves it can name
    /// are the settings pair, and no invocation carries an install flag.
    func testTheClientNeverRunsAnInstall() async {
        let process = RecordingProcessRunner([
            .success(OcrProcessResult(stdout: envelope(), stderr: "", code: 0, truncated: false)),
            .success(OcrProcessResult(
                stdout: envelope(embedded: true, revision: 1), stderr: "", code: 0, truncated: false
            )),
        ])
        let client = runner(process)
        _ = await client.read()
        _ = await client.update(revision: 0, change: .embeddedImageOcr(true))

        for arguments in process.invocations {
            XCTAssertEqual(arguments[6], "document")
            XCTAssertTrue(["settings", "configure"].contains(arguments[7]))
            XCTAssertFalse(arguments.contains { $0.hasPrefix("--ocr") })
            XCTAssertFalse(arguments.contains("install"))
            XCTAssertFalse(arguments.contains("--payload-file"))
        }
    }

    // MARK: Honest failures

    func testARefusalIsReportedByItsFrozenCode() async {
        // `--json` prints the error envelope and exits by the code's mapping,
        // so the exit status is not what decides this.
        let process = RecordingProcessRunner(stdout: refusal("conflict"), code: 5)
        let result = await runner(process).update(revision: 1, change: .mode(.textOnly))

        XCTAssertEqual(result.failure, .refused(code: DocumentProcessingError.conflict))
        XCTAssertTrue(result.failure!.sentence.contains("changed elsewhere"))
    }

    func testANewerContractVersionIsRefusedRatherThanReadAsThisOne() async {
        let process = RecordingProcessRunner(stdout: envelope(apiVersion: "1.4"))
        let result = await runner(process).read()
        XCTAssertEqual(result.failure, .unsupportedContract("1.4"))
    }

    func testASpawnFailureIsUnavailableAndATimeoutIsATimeout() async {
        let spawn = await runner(RecordingProcessRunner([.failure(.spawnFailed)])).read()
        XCTAssertEqual(spawn.failure, .unavailable)
        let expired = await runner(RecordingProcessRunner([.failure(.timedOut)])).read()
        XCTAssertEqual(expired.failure, .timedOut)
    }

    /// The client's own local refusals carry no envelope. Each is a state of
    /// its own, and none of them is "Core answered something unreadable" — nor
    /// is any of them rendered as a preference that was changed.
    func testALocalRefusalWithNoEnvelopeIsReportedByItsExitStatus() async {
        for (code, fragment) in [
            (Int32(1), "may not be running"),
            (Int32(2), "refused the call here"),
            (Int32(3), "credential"),
            (Int32(4), "agree on a version"),
            (Int32(6), "ran out of time"),
        ] {
            let process = RecordingProcessRunner(stdout: "", code: code)
            let result = await runner(process).update(revision: 1, change: .mode(.textOnly))
            XCTAssertEqual(result.failure, .noAnswer(exitCode: code))
            XCTAssertTrue(
                result.failure!.sentence.contains(fragment),
                "exit \(code): \(result.failure!.sentence)"
            )
        }
    }

    /// Exit zero with output that is not an envelope is the other thing: Core
    /// answered, and the answer could not be read.
    func testAnUnreadableAnswerAtExitZeroStaysUnreadable() async {
        let process = RecordingProcessRunner(stdout: "{}", code: 0)
        let result = await runner(process).read()
        XCTAssertEqual(result.failure, .unreadableOutput)
    }

    func testATruncatedDocumentIsNotRead() async {
        let process = RecordingProcessRunner(stdout: envelope(), truncated: true)
        let result = await runner(process).read()
        XCTAssertEqual(result.failure, .unreadableOutput)
    }

    func testAnEnvelopeWithBothBranchesOrNeitherIsUnreadable() {
        XCTAssertEqual(
            DocumentProcessingCommandRunner.decode(
                """
                {"error": {"code": "conflict"}, "metadata": {"version": {"api_version": "1.3"}}, \
                "result": {"settings": {}}}
                """
            ).failure,
            .unreadableOutput
        )
        XCTAssertEqual(
            DocumentProcessingCommandRunner.decode(
                "{\"metadata\": {\"version\": {\"api_version\": \"1.3\"}}}"
            ).failure,
            .unreadableOutput
        )
        XCTAssertEqual(DocumentProcessingCommandRunner.decode("not json").failure, .unreadableOutput)
    }

    /// The contract's bounds, held rather than trusted: a document outside them
    /// is not rendered as a preference anybody chose.
    func testTheSettingsMemberIsBoundedInEveryDirection() {
        XCTAssertNil(DocumentProcessingCommandRunner.settings(from: [
            "mode": "text_only", "recognition_languages": [], "embedded_image_ocr": false,
            "revision": 1,
        ]))
        XCTAssertNil(DocumentProcessingCommandRunner.settings(from: [
            "mode": "text_only", "recognition_languages": ["eng", "deu", "fra", "ita"],
            "embedded_image_ocr": false, "revision": 1,
        ]))
        XCTAssertNil(DocumentProcessingCommandRunner.settings(from: [
            "mode": "text_only", "recognition_languages": ["eng", "eng"],
            "embedded_image_ocr": false, "revision": 1,
        ]))
        XCTAssertNil(DocumentProcessingCommandRunner.settings(from: [
            "mode": "text_only", "recognition_languages": ["eng"],
            "embedded_image_ocr": "false", "revision": 1,
        ]))
        XCTAssertNil(DocumentProcessingCommandRunner.settings(from: [
            "mode": "text_only", "recognition_languages": ["eng"],
            "embedded_image_ocr": false, "revision": -1,
        ]))
        XCTAssertNil(DocumentProcessingCommandRunner.settings(from: [
            "mode": "Text Only", "recognition_languages": ["eng"],
            "embedded_image_ocr": false, "revision": 1,
        ]))
        XCTAssertNil(DocumentProcessingCommandRunner.settings(from: [
            "mode": "text_only", "recognition_languages": ["--ocr-payload"],
            "embedded_image_ocr": false, "revision": 1,
        ]))
    }

    /// A decoded code is never something that could be spent as a flag, a path
    /// or a language name it is not.
    func testACodeIsOnlyAcceptedInTheBoundedTokenShape() {
        for accepted in ["eng", "chi_sim", "text_and_local_ocr", "a", "a1", "a.b"] {
            XCTAssertTrue(DocumentProcessingCommandRunner.isOpenCode(accepted), accepted)
        }
        for refused in [
            "", "-eng", "--input-json", "/etc/passwd", "ENG", "1eng", "eng ", "en g", "eng_",
            "eng.", "éng", String(repeating: "e", count: 129),
        ] {
            XCTAssertFalse(DocumentProcessingCommandRunner.isOpenCode(refused), refused)
        }
    }

    /// The vocabulary is open: a mode this build has never heard of is carried
    /// as itself rather than becoming one of the two it knows.
    func testAnUnknownModeIsKeptAndNotResolvedToAKnownOne() async {
        let process = RecordingProcessRunner(stdout: envelope(mode: "text_and_remote_ocr"))
        let result = await runner(process).read()

        XCTAssertEqual((try? result.get())?.modeCode, "text_and_remote_ocr")
        XCTAssertNil((try? result.get())?.mode ?? nil)
    }

    /// The client refuses to exist at all without two absolute paths and a
    /// workspace to read: the CLI rejects a relative installation state, and a
    /// client that cannot name the workspace it reads has nothing truthful to
    /// show.
    func testARelativePathOrAnUnnamedWorkspaceHasNoClient() throws {
        XCTAssertNil(DocumentProcessingCommandRunner(
            executable: executable,
            installationState: try XCTUnwrap(URL(string: "state")),
            workspaceRef: workspace
        ))
        XCTAssertNil(DocumentProcessingCommandRunner(
            executable: try XCTUnwrap(URL(string: "https://example.invalid/omnivia")),
            installationState: installationState,
            workspaceRef: workspace
        ))
        XCTAssertNil(DocumentProcessingCommandRunner(
            executable: executable, installationState: installationState, workspaceRef: ""
        ))
    }
}

// MARK: - The pane's model

@MainActor
final class DocumentProcessingModelTests: XCTestCase {
    // MARK: States

    /// No installed client: the pane says so, and nothing is ever run.
    func testWithoutAnAdministratorThereIsNothingToRead() async {
        let model = DocumentProcessingModel(administering: nil)
        XCTAssertEqual(model.state, .noAdministrator)
        XCTAssertFalse(model.canAct)
        await model.load()
        XCTAssertEqual(model.state, .noAdministrator)
        XCTAssertNil(model.settings)
    }

    /// The pane renders what Core holds, including that nothing is stored yet.
    func testTheCurrentSettingsAreReadAndRendered() async {
        let stub = StubAdministrator(reads: [.success(settings())])
        let model = DocumentProcessingModel(administering: stub)
        XCTAssertEqual(model.state, .loading)

        await model.load()
        XCTAssertEqual(model.state, .ready(settings()))
        XCTAssertEqual(model.settings?.mode, .textAndLocalOcr)
        XCTAssertEqual(model.settings?.embeddedImageOcr, false)
        XCTAssertTrue(model.settings!.isProductDefault)
        XCTAssertTrue(model.canAct)
    }

    func testAFirstFailedReadIsUnavailableNotADefault() async {
        let stub = StubAdministrator(reads: [.failure(.unavailable)])
        let model = DocumentProcessingModel(administering: stub)
        await model.load()

        XCTAssertEqual(model.state, .unavailable(.unavailable))
        XCTAssertNil(model.settings)
        XCTAssertFalse(model.canAct)
    }

    /// A failed *re*-read keeps the previous values and marks them previous.
    func testAFailedRereadLeavesThePreviousReadingMarkedStale() async {
        let stub = StubAdministrator(reads: [.success(settings()), .failure(.timedOut)])
        let model = DocumentProcessingModel(administering: stub)
        await model.load()
        await model.refresh()

        XCTAssertEqual(model.state, .ready(settings()))
        XCTAssertEqual(model.staleError, .timedOut)
    }

    func testLoadIsIdempotentUntilForced() async {
        let stub = StubAdministrator(reads: [.success(settings()), .success(settings())])
        let model = DocumentProcessingModel(administering: stub)
        await model.load()
        await model.load()
        XCTAssertEqual(stub.calls, [.read])
    }

    // MARK: Writes

    func testAProfileChangePersistsThroughTheReadRevisionAndIsReadBack() async {
        let stored = settings(mode: "text_only", revision: 8)
        let stub = StubAdministrator(
            reads: [.success(settings(revision: 7)), .success(stored)],
            writes: [.success(stored)]
        )
        let model = DocumentProcessingModel(administering: stub)
        await model.load()

        let result = await model.apply(.mode(.textOnly))
        XCTAssertNotNil(try? result.get())
        XCTAssertEqual(stub.calls, [
            .read, .update(revision: 7, change: .mode(.textOnly)), .read,
        ])
        XCTAssertEqual(model.state, .ready(stored))
        XCTAssertNil(model.busy)
        XCTAssertNil(model.lastWriteError)
    }

    func testTheEmbeddedImageTogglePersistsAndIsOffByDefault() async {
        let stored = settings(embedded: true, revision: 1)
        let stub = StubAdministrator(
            reads: [.success(settings()), .success(stored)],
            writes: [.success(stored)]
        )
        let model = DocumentProcessingModel(administering: stub)
        await model.load()
        XCTAssertEqual(model.settings?.embeddedImageOcr, false)

        await model.apply(.embeddedImageOcr(true))
        XCTAssertEqual(stub.calls, [
            .read, .update(revision: 0, change: .embeddedImageOcr(true)), .read,
        ])
        XCTAssertEqual(model.settings?.embeddedImageOcr, true)
    }

    /// A control that already says this sends nothing: a two-segment control
    /// fires on the segment that is already selected.
    func testAChangeAlreadyInForceIsNotWritten() async {
        let stub = StubAdministrator(reads: [.success(settings())])
        let model = DocumentProcessingModel(administering: stub)
        await model.load()

        await model.apply(.mode(.textAndLocalOcr))
        await model.apply(.embeddedImageOcr(false))
        XCTAssertEqual(stub.calls, [.read])
    }

    /// A failed write keeps the failure beside the value Core actually holds —
    /// never the value that was clicked and not saved.
    func testAFailedWriteLeavesTheStoredValueAndTheReason() async {
        let stub = StubAdministrator(
            reads: [.success(settings(revision: 3)), .success(settings(revision: 3))],
            writes: [.failure(.refused(code: DocumentProcessingError.conflict))]
        )
        let model = DocumentProcessingModel(administering: stub)
        await model.load()

        let result = await model.apply(.mode(.textOnly))
        XCTAssertEqual(result.failure, .refused(code: DocumentProcessingError.conflict))
        XCTAssertEqual(model.lastWriteError, .refused(code: DocumentProcessingError.conflict))
        // Re-read, and the stored value is what is on screen.
        XCTAssertEqual(stub.calls, [
            .read, .update(revision: 3, change: .mode(.textOnly)), .read,
        ])
        XCTAssertEqual(model.settings?.mode, .textAndLocalOcr)
        XCTAssertNil(model.busy)
    }

    /// A write that timed out may have landed, so the answer comes from a fresh
    /// read rather than from an assumption either way.
    func testATimedOutWriteIsStillFollowedByAFreshRead() async {
        let landed = settings(mode: "text_only", revision: 4)
        let stub = StubAdministrator(
            reads: [.success(settings(revision: 3)), .success(landed)],
            writes: [.failure(.timedOut)]
        )
        let model = DocumentProcessingModel(administering: stub)
        await model.load()
        await model.apply(.mode(.textOnly))

        XCTAssertEqual(model.lastWriteError, .timedOut)
        XCTAssertEqual(model.state, .ready(landed))
    }

    /// Without a reading there is no revision to write under, and this build
    /// will not guess one — the compare-and-swap cannot catch a guess.
    func testNothingIsWrittenWithoutAReadingToWriteOver() async {
        let stub = StubAdministrator(reads: [.failure(.unavailable)])
        let model = DocumentProcessingModel(administering: stub)
        await model.load()

        let refused = await model.apply(.mode(.textOnly))
        XCTAssertEqual(refused.failure, .unavailable)
        XCTAssertEqual(stub.calls, [.read])

        let absent = DocumentProcessingModel(administering: nil)
        let unavailable = await absent.apply(.mode(.textOnly))
        XCTAssertEqual(unavailable.failure, .unavailable)
    }

    func testAWriteInFlightBlocksTheOtherControls() async {
        let stub = StubAdministrator(
            reads: [.success(settings()), .success(settings(revision: 1))],
            writes: [.success(settings(revision: 1))]
        )
        let model = DocumentProcessingModel(administering: stub)
        await model.load()

        var busyDuring: [String?] = []
        model.onChange = { busyDuring.append(model.busy) }
        await model.apply(.embeddedImageOcr(true))

        XCTAssertEqual(busyDuring.first, DocumentProcessingChange.embeddedImageOcr(true).key)
        XCTAssertNil(model.busy)
        XCTAssertTrue(model.canAct)
    }

    // MARK: The selected languages

    /// English is here because the service reported it, and it is reported as
    /// installed only when the managed cache says so.
    func testTheSelectedLanguagesAreOrderedAndMarkedAgainstTheCache() async {
        let selected = DocumentProcessingModel.selectedLanguages(
            settings(languages: ["deu", "eng"]), installed: inventory(["eng"])
        )
        XCTAssertEqual(selected.map(\.code), ["deu", "eng"])
        XCTAssertEqual(selected.map(\.displayName), ["German", "English"])
        XCTAssertEqual(selected.map(\.availability), [.notInstalled, .installed])
        XCTAssertEqual(selected[0].status, "Selected, not installed")
    }

    /// English is never invented. A default selection of `eng` against an empty
    /// cache is a selected language that is not installed, not English present.
    func testEnglishIsNotInventedWhenNothingIsInstalled() {
        let selected = DocumentProcessingModel.selectedLanguages(
            settings(), installed: inventory([])
        )
        XCTAssertEqual(selected.map(\.code), ["eng"])
        XCTAssertEqual(selected.map(\.availability), [.notInstalled])
    }

    /// An inventory that could not be read is not permission to say a language
    /// is missing.
    func testAnUnreadableInventoryLeavesTheSelectionUnconfirmed() {
        let selected = DocumentProcessingModel.selectedLanguages(settings(), installed: nil)
        XCTAssertEqual(selected.map(\.availability), [.unconfirmed])
        XCTAssertEqual(selected[0].status, "Installed languages couldn’t be read")
    }

    /// A language code this build has no name for renders as itself rather than
    /// as an invented language name.
    func testAnUnknownLanguageCodeRendersAsItself() {
        let selected = DocumentProcessingModel.selectedLanguages(
            settings(languages: ["zxx"]), installed: inventory([])
        )
        XCTAssertEqual(selected[0].displayName, "zxx")
    }

    // MARK: What the pane states

    /// The preferences decide what a future capture is admitted under, and an
    /// existing capture changes only through the explicit reprocess action —
    /// which this screen names, because it has no selected source to act on.
    func testTheFutureCaptureWordingAndTheReprocessRouteAreStated() {
        let note = DocumentProcessingModel.futureCapturesNote
        XCTAssertTrue(note.contains("future captures"))
        XCTAssertTrue(note.contains("reprocesses nothing"))
        XCTAssertTrue(note.contains("Reprocess captured copy"))
    }

    /// Selecting a language is not installing one.
    func testSelectingALanguageIsStatedNotToInstallAnything() {
        XCTAssertTrue(
            DocumentProcessingModel.noInstallNote.contains("never downloads or installs")
        )
    }

    /// Changing a preference reaches the settings authority and nothing else:
    /// no managed-resource call, so no install can be a side effect of it.
    func testChangingAPreferenceTouchesNoResourceOwner() async {
        let resources = InertResourceAdministrator()
        let languages = OcrLanguageModel(administering: resources)
        await languages.load()

        let stub = StubAdministrator(
            reads: [.success(settings()), .success(settings(embedded: true, revision: 1))],
            writes: [.success(settings(embedded: true, revision: 1))]
        )
        let model = DocumentProcessingModel(administering: stub)
        await model.load()
        await model.apply(.embeddedImageOcr(true))

        // The one passive read the pane opens with, and nothing more.
        XCTAssertEqual(resources.calls, ["status"])
    }
}

/// A managed-resource client that records what it was asked for. Any call other
/// than the pane's opening `status` read would be a resource action a preference
/// change caused, which is what this is here to catch.
private final class InertResourceAdministrator: OcrResourceAdministering, @unchecked Sendable {
    private(set) var calls: [String] = []

    func status() async -> Result<OcrResourceInventory, OcrResourceError> {
        calls.append("status")
        return .success(inventory([]))
    }

    func remove(
        _ identity: OcrResourceIdentity
    ) async -> Result<OcrResourceInventory, OcrResourceError> {
        calls.append("remove")
        return .success(inventory([]))
    }

    func reconcile() async -> Result<OcrReconcileOutcome, OcrResourceError> {
        calls.append("reconcile")
        return .failure(.unreadableOutput)
    }

    func install(
        payload: URL, identity: OcrResourceIdentity
    ) async -> Result<OcrResourceInventory, OcrResourceError> {
        calls.append("install")
        return .failure(.unreadableOutput)
    }
}

extension Result {
    fileprivate var failure: Failure? {
        guard case .failure(let error) = self else { return nil }
        return error
    }
}
