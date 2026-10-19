// The update check behind General > Updates.
//
// The documents are what `omnivia service update-check --json` prints
// (`sort_keys=True`, `ok` added by the CLI). The process is injected, so the
// argument vector, the bounds and the strict decoding are checked without a
// client on the machine. The model tests need no AppKit.

import Foundation
import XCTest

@testable import CoreSettingsMacOS

// MARK: - Fixtures

private let executable = URL(fileURLWithPath: "/Applications/OmniVia/bin/omnivia")
private let installationState = URL(fileURLWithPath: "/Users/someone/Library/omnivia/state")
private let workspace = "workspace-1"
private let bundle = "sha256:" + String(repeating: "ab", count: 32)
private let checkedAtText = "2026-10-20T09:30:00Z"
private let checkedAt = ISO8601DateFormatter().date(from: checkedAtText)!

/// A document built member by member, so one test changes one member.
private func document(
    status: String,
    ok: Bool? = nil,
    version: String = "1",
    extra: [String: String] = [:],
    drop: Set<String> = []
) -> String {
    let failure = status == "check_failed" || status == "unsupported_install"
    var members: [String: String] = [
        "update_check_adapter_version": version,
        "product_id": "\"omnivia-core\"",
        "status": "\"\(status)\"",
        "installed": "{\"omnivia-core\": \"0.4.0\"}",
        "checked_at": "\"\(checkedAtText)\"",
        "channel_url": "\"https://example.test/channel.json\"",
        "ok": String(ok ?? !failure),
    ]
    switch status {
    case "update_available":
        members["candidate_version"] = "\"0.5.0\""
        members["release_url"] = "\"https://example.test/release\""
        members["bundle_sha256"] = "\"\(bundle)\""
    case "check_failed", "unsupported_install":
        members["reason"] = "\"channel unreachable\""
    default: break
    }
    for (key, value) in extra { members[key] = value }
    for key in drop { members[key] = nil }
    return "{" + members.sorted { $0.key < $1.key }
        .map { "\"\($0.key)\": \($0.value)" }.joined(separator: ", ") + "}\n"
}

private final class StubProcess: OcrProcessRunning, @unchecked Sendable {
    struct Invocation: Equatable {
        let executable: URL
        let arguments: [String]
        let timeout: TimeInterval
        let stdoutLimit: Int
        let stderrLimit: Int
    }

    private(set) var invocations: [Invocation] = []
    private let outcome: Result<OcrProcessResult, OcrProcessFailure>

    init(_ outcome: Result<OcrProcessResult, OcrProcessFailure>) { self.outcome = outcome }

    convenience init(stdout: String, code: Int32 = 0, stderr: String = "", truncated: Bool = false) {
        self.init(.success(OcrProcessResult(
            stdout: stdout, stderr: stderr, code: code, truncated: truncated
        )))
    }

    func run(
        executable: URL, arguments: [String], timeout: TimeInterval,
        stdoutLimit: Int, stderrLimit: Int
    ) async -> Result<OcrProcessResult, OcrProcessFailure> {
        invocations.append(Invocation(
            executable: executable, arguments: arguments, timeout: timeout,
            stdoutLimit: stdoutLimit, stderrLimit: stderrLimit
        ))
        return outcome
    }
}

private func runner(_ process: StubProcess, timeout: TimeInterval = 5) -> CoreUpdateCommandRunner {
    CoreUpdateCommandRunner(
        executable: executable, installationState: installationState,
        workspaceRef: workspace, timeout: timeout, process: process
    )!
}

private func check(
    stdout: String, code: Int32 = 0
) async -> Result<CoreUpdateCheckResult, CoreUpdateError> {
    await runner(StubProcess(stdout: stdout, code: code)).check()
}

/// An invalid document is refused whole. With exit 0 that is unreadable output;
/// with a non-zero exit there is no adapter document at all, so it is `noAnswer`.
private func refused(_ code: Int32) -> Result<CoreUpdateCheckResult, CoreUpdateError> {
    .failure(code == 0 ? .unreadableOutput : .noAnswer(exitCode: code))
}

// MARK: - The command

final class CoreUpdateAdministrationTests: XCTestCase {
    func testExactArgvExecutableAndBounds() async {
        let process = StubProcess(stdout: document(status: "up_to_date"))
        _ = await runner(process, timeout: 7).check()
        XCTAssertEqual(process.invocations, [StubProcess.Invocation(
            executable: executable,
            arguments: [
                "--installation-state", "/Users/someone/Library/omnivia/state",
                "--workspace-id", "workspace-1",
                "service", "update-check", "--json",
            ],
            timeout: 7,
            stdoutLimit: 64 << 10,
            stderrLimit: 64 << 10
        )])
    }

    func testArgvHasNoInstallOrAutomaticVerb() {
        let argv = CoreUpdateCommandRunner.arguments(
            installationState: installationState, workspaceRef: workspace
        )
        for forbidden in ["update-apply", "update-install", "apply", "install", "--automatic"] {
            XCTAssertFalse(argv.contains(forbidden))
        }
        XCTAssertEqual(argv.suffix(3), ["service", "update-check", "--json"])
    }

    func testRefusesRelativePathsAndEmptyWorkspace() {
        XCTAssertNil(CoreUpdateCommandRunner(
            executable: URL(string: "omnivia")!, installationState: installationState,
            workspaceRef: workspace
        ))
        XCTAssertNil(CoreUpdateCommandRunner(
            executable: executable, installationState: URL(string: "relative/state")!,
            workspaceRef: workspace
        ))
        XCTAssertNil(CoreUpdateCommandRunner(
            executable: executable, installationState: installationState, workspaceRef: ""
        ))
    }

    // MARK: Six statuses

    func testUpToDate() async {
        let result = await check(stdout: document(status: "up_to_date"))
        XCTAssertEqual(result, .success(CoreUpdateCheckResult(status: .upToDate, checkedAt: checkedAt)))
    }

    func testUpdateAvailable() async {
        let result = await check(stdout: document(status: "update_available"))
        XCTAssertEqual(result, .success(CoreUpdateCheckResult(
            status: .updateAvailable, checkedAt: checkedAt, candidateVersion: "0.5.0"
        )))
    }

    func testUpdateAvailableWithoutBundleDigest() async {
        let result = await check(stdout: document(status: "update_available", drop: ["bundle_sha256"]))
        XCTAssertEqual(try? result.get().candidateVersion, "0.5.0")
    }

    func testAheadOfChannel() async {
        let result = await check(stdout: document(status: "ahead_of_channel"))
        XCTAssertEqual(try? result.get().status, .aheadOfChannel)
    }

    func testNoRelease() async {
        let result = await check(stdout: document(status: "no_release"))
        XCTAssertEqual(try? result.get().status, .noRelease)
    }

    func testCheckFailedIsAnAnswerDespiteExitOne() async {
        let result = await check(stdout: document(status: "check_failed"), code: 1)
        XCTAssertEqual(result, .success(CoreUpdateCheckResult(
            status: .checkFailed, checkedAt: checkedAt, reason: "channel unreachable"
        )))
    }

    func testUnsupportedInstallIsAnAnswerDespiteExitOne() async {
        let result = await check(stdout: document(status: "unsupported_install"), code: 1)
        XCTAssertEqual(result, .success(CoreUpdateCheckResult(
            status: .unsupportedInstall, checkedAt: checkedAt, reason: "channel unreachable"
        )))
    }

    func testAllSixStatusesAreCovered() {
        XCTAssertEqual(CoreUpdateStatus.allCases.count, 6)
        XCTAssertEqual(
            Set(CoreUpdateStatus.allCases.map(\.rawValue)),
            ["up_to_date", "update_available", "ahead_of_channel", "no_release",
             "check_failed", "unsupported_install"]
        )
    }

    // MARK: Exit status vs document

    func testExitCodeContradictingTheDocumentIsUnreadable() async {
        let nonzeroSuccess = await check(stdout: document(status: "up_to_date"), code: 1)
        XCTAssertEqual(nonzeroSuccess, .failure(.unreadableOutput))
        let zeroFailure = await check(stdout: document(status: "check_failed"), code: 0)
        XCTAssertEqual(zeroFailure, .failure(.unreadableOutput))
    }

    func testNonzeroWithoutAdapterDocumentIsNoAnswer() async {
        for stdout in ["", "usage: omnivia ...\n", "{}"] {
            let result = await check(stdout: stdout, code: 2)
            XCTAssertEqual(result, .failure(.noAnswer(exitCode: 2)), stdout)
        }
        XCTAssertTrue(CoreUpdateError.noAnswer(exitCode: 2).sentence.contains("status 2"))
    }

    // MARK: Strict decoding

    func testUnknownStatusIsUnreadable() async {
        let result = await check(stdout: document(status: "installing", ok: true))
        XCTAssertEqual(result, .failure(.unreadableOutput))
    }

    func testUnknownMemberIsUnreadable() async {
        for status in ["up_to_date", "update_available", "check_failed"] {
            let result = await check(
                stdout: document(status: status, extra: ["install_now": "true"]),
                code: status == "check_failed" ? 1 : 0
            )
            XCTAssertEqual(result, refused(status == "check_failed" ? 1 : 0), status)
        }
    }

    func testMissingRequiredMemberIsUnreadable() async {
        for key in ["update_check_adapter_version", "product_id", "status", "installed",
                    "checked_at", "channel_url", "ok"] {
            let result = await check(stdout: document(status: "up_to_date", drop: [key]))
            XCTAssertEqual(result, .failure(.unreadableOutput), key)
        }
    }

    func testWrongTypesAndValuesAreUnreadable() async {
        let cases: [String: [String: String]] = [
            "version 2": ["update_check_adapter_version": "2"],
            "version bool": ["update_check_adapter_version": "true"],
            "version string": ["update_check_adapter_version": "\"1\""],
            "other product": ["product_id": "\"other\""],
            "ok as number": ["ok": "1"],
            "ok as string": ["ok": "\"true\""],
            "status null": ["status": "null"],
            "checked_at no zone": ["checked_at": "\"2026-10-20T09:30:00\""],
            "checked_at junk": ["checked_at": "\"yesterday\""],
            "channel_url null": ["channel_url": "null"],
            "installed array": ["installed": "[]"],
            "installed non-string": ["installed": "{\"omnivia-core\": 4}"],
        ]
        for (name, extra) in cases {
            let result = await check(stdout: document(status: "up_to_date", extra: extra))
            XCTAssertEqual(result, .failure(.unreadableOutput), name)
        }
    }

    func testContradictoryFieldsAreUnreadable() async {
        let cases: [(String, String, [String: String], Set<String>, Int32)] = [
            ("ok false on up_to_date", "up_to_date", ["ok": "false"], [], 0),
            ("ok true on check_failed", "check_failed", ["ok": "true"], [], 1),
            ("candidate on up_to_date", "up_to_date", ["candidate_version": "\"0.5.0\""], [], 0),
            ("release_url on no_release", "no_release",
             ["release_url": "\"https://example.test/r\""], [], 0),
            ("bundle on ahead", "ahead_of_channel", ["bundle_sha256": "\"\(bundle)\""], [], 0),
            ("reason on up_to_date", "up_to_date", ["reason": "\"fine\""], [], 0),
            ("reason on update_available", "update_available", ["reason": "\"x\""], [], 0),
            ("no candidate on update_available", "update_available", [:], ["candidate_version"], 0),
            ("no release_url on update_available", "update_available", [:], ["release_url"], 0),
            ("non-https release_url", "update_available",
             ["release_url": "\"http://example.test/r\""], [], 0),
            ("bad candidate version", "update_available",
             ["candidate_version": "\"0.05.0\""], [], 0),
            ("bad bundle digest", "update_available",
             ["bundle_sha256": "\"sha256:ABC\""], [], 0),
            ("no reason on check_failed", "check_failed", [:], ["reason"], 1),
            ("empty reason on check_failed", "check_failed", ["reason": "\"\""], [], 1),
            ("candidate on check_failed", "check_failed",
             ["candidate_version": "\"0.5.0\""], [], 1),
            ("release_url on unsupported_install", "unsupported_install",
             ["release_url": "\"https://example.test/r\""], [], 1),
        ]
        for (name, status, extra, drop, code) in cases {
            let result = await check(
                stdout: document(status: status, extra: extra, drop: drop), code: code
            )
            XCTAssertEqual(result, refused(code), name)
        }
    }

    func testMalformedJSONIsUnreadable() async {
        let truncated = String(document(status: "up_to_date").dropLast(3))
        for stdout in ["", "not json", truncated, "[]", "null", "\"up_to_date\"",
                       document(status: "up_to_date") + document(status: "up_to_date")] {
            let result = await check(stdout: stdout)
            XCTAssertEqual(result, .failure(.unreadableOutput), stdout)
        }
    }

    func testOverlongReasonIsUnreadable() async {
        let long = String(repeating: "a", count: 4097)
        let result = await check(
            stdout: document(status: "check_failed", extra: ["reason": "\"\(long)\""]), code: 1
        )
        XCTAssertEqual(result, refused(1))
    }

    // MARK: Process failures

    func testTimeout() async {
        let result = await runner(StubProcess(.failure(.timedOut))).check()
        XCTAssertEqual(result, .failure(.timedOut))
    }

    func testSpawnFailure() async {
        let result = await runner(StubProcess(.failure(.spawnFailed))).check()
        XCTAssertEqual(result, .failure(.unavailable))
    }

    func testTruncatedOutputIsNotParsedEvenWhenItLooksValid() async {
        let result = await runner(StubProcess(
            stdout: document(status: "up_to_date"), truncated: true
        )).check()
        XCTAssertEqual(result, .failure(.outputTooLarge))
        let nonzero = await runner(StubProcess(
            stdout: document(status: "check_failed"), code: 1, truncated: true
        )).check()
        XCTAssertEqual(nonzero, .failure(.outputTooLarge))
    }

    // MARK: Display

    func testSafeReasonFoldsControlsAndBoundsLength() {
        let messy = CoreUpdateCheckResult(
            status: .checkFailed, checkedAt: checkedAt, reason: "  a\n\tb\u{7}  c  "
        )
        XCTAssertEqual(messy.safeReason, "a b c")
        let long = CoreUpdateCheckResult(
            status: .checkFailed, checkedAt: checkedAt, reason: String(repeating: "x", count: 500)
        )
        XCTAssertEqual(long.safeReason?.count, CoreUpdateCheckResult.reasonDisplayLimit + 1)
        XCTAssertTrue(long.safeReason!.hasSuffix("…"))
        XCTAssertNil(CoreUpdateCheckResult(status: .upToDate, checkedAt: checkedAt).safeReason)
    }

    func testEverySentenceStatesNothingWasChangedOrNoRoute() {
        let errors: [CoreUpdateError] = [
            .unavailable, .timedOut, .unreadableOutput, .outputTooLarge, .noAnswer(exitCode: 3),
        ]
        for error in errors {
            XCTAssertTrue(
                error.sentence.contains("Nothing was changed") || error == .unavailable,
                error.sentence
            )
        }
    }
}

// MARK: - The pane's model

/// Counts calls and holds each one until released, so a check can be observed
/// while it is in flight.
private final class GatedAdministering: CoreUpdateAdministering, @unchecked Sendable {
    private let lock = NSLock()
    private var calls = 0
    private var pending: [CheckedContinuation<Result<CoreUpdateCheckResult, CoreUpdateError>, Never>] = []

    var callCount: Int { lock.withLock { calls } }

    func check() async -> Result<CoreUpdateCheckResult, CoreUpdateError> {
        await withCheckedContinuation { continuation in
            lock.withLock {
                calls += 1
                pending.append(continuation)
            }
        }
    }

    /// Resumes the oldest held call. Returns false, rather than trapping, when
    /// no call is held yet.
    @discardableResult
    func release(_ result: Result<CoreUpdateCheckResult, CoreUpdateError>) -> Bool {
        guard let continuation = lock.withLock({ pending.isEmpty ? nil : pending.removeFirst() })
        else { return false }
        continuation.resume(returning: result)
        return true
    }
}

@MainActor
final class CoreUpdateAdministrationModelTests: XCTestCase {
    private let available = CoreUpdateCheckResult(
        status: .updateAvailable, checkedAt: checkedAt, candidateVersion: "0.5.0"
    )
    private let upToDate = CoreUpdateCheckResult(status: .upToDate, checkedAt: checkedAt)

    func testInitialStateRunsNothing() {
        let gate = GatedAdministering()
        let model = CoreUpdateCheckModel(administering: gate)
        XCTAssertEqual(model.state, .neverChecked)
        XCTAssertTrue(model.canCheck)
        XCTAssertEqual(gate.callCount, 0, "nothing checks on creation: manual only")
    }

    func testNoRouteIsUnavailableAndOffersNoCheck() async {
        let model = CoreUpdateCheckModel(administering: nil)
        XCTAssertEqual(model.state, .failed(.unavailable))
        XCTAssertFalse(model.canCheck)
        let ran = await model.checkNow()
        XCTAssertFalse(ran)
        XCTAssertEqual(model.state, .failed(.unavailable))
    }

    func testCheckNowMovesThroughCheckingToResult() async {
        let gate = GatedAdministering()
        let model = CoreUpdateCheckModel(administering: gate)
        var seen: [CoreUpdateCheckModel.State] = []
        model.onChange = { seen.append(model.state) }

        let task = Task { @MainActor in await model.checkNow() }
        while gate.callCount == 0 { await Task.yield() }
        XCTAssertEqual(model.state, .checking)
        XCTAssertFalse(model.canCheck)

        gate.release(.success(available))
        let ran = await task.value
        XCTAssertTrue(ran)
        XCTAssertEqual(model.state, .result(available))
        XCTAssertTrue(model.canCheck)
        XCTAssertEqual(seen, [.checking, .result(available)])
    }

    func testNoConcurrencyWhileChecking() async {
        let gate = GatedAdministering()
        let model = CoreUpdateCheckModel(administering: gate)
        let first = Task { @MainActor in await model.checkNow() }
        while gate.callCount == 0 { await Task.yield() }

        let second = await model.checkNow()
        let third = await model.checkNow()
        XCTAssertFalse(second)
        XCTAssertFalse(third)
        XCTAssertEqual(gate.callCount, 1, "a second click must not start a second child")

        gate.release(.success(upToDate))
        let ranFirst = await first.value
        XCTAssertTrue(ranFirst)
        XCTAssertEqual(gate.callCount, 1)
    }

    func testNewerFailureReplacesOlderResult() async {
        let gate = GatedAdministering()
        let replacing = CoreUpdateCheckModel(administering: gate)
        let outcomes: [Result<CoreUpdateCheckResult, CoreUpdateError>] = [
            .success(available), .failure(.timedOut), .success(upToDate),
            .failure(.noAnswer(exitCode: 2)),
        ]
        for (index, outcome) in outcomes.enumerated() {
            let task = Task { @MainActor in await replacing.checkNow() }
            // The stub counts the call and holds its continuation under one lock,
            // so this iteration's count means this iteration's call is releasable.
            while gate.callCount < index + 1 { await Task.yield() }
            XCTAssertEqual(replacing.state, .checking)
            XCTAssertTrue(gate.release(outcome))
            _ = await task.value
            switch outcome {
            case .success(let result): XCTAssertEqual(replacing.state, .result(result))
            case .failure(let error): XCTAssertEqual(replacing.state, .failed(error))
            }
            XCTAssertTrue(replacing.canCheck)
        }
        XCTAssertEqual(gate.callCount, 4)
    }

    func testFailedCheckCanBeRetried() async {
        let gate = GatedAdministering()
        let model = CoreUpdateCheckModel(administering: gate)
        let first = Task { @MainActor in await model.checkNow() }
        while gate.callCount < 1 { await Task.yield() }
        gate.release(.failure(.unreadableOutput))
        _ = await first.value
        XCTAssertEqual(model.state, .failed(.unreadableOutput))
        XCTAssertTrue(model.canCheck)
    }
}
