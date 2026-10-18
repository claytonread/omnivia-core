// What `settings list` and `settings set` hand back, and what they are asked.
//
// Two halves worth separate tests. The *arguments* matter because a settings
// call that forgot `--workspace-id` would reach the wrong service's authority,
// and because a key or a value carrying a quote has to survive becoming a JSON
// document. The *decoding* matters because a row missing a field must make the
// answer unreadable rather than silently shorter.
//
// These drive `SettingsCommandRunner` against a stand-in executable rather than
// a mock, so the pipe draining and exit-code reading are exercised too.

import Foundation
import XCTest

@testable import CoreSettingsMacOS

final class PersistedSettingsTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("settings-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    /// A shell script standing in for the installed `omnivia`. It records the
    /// arguments it was handed so the invocation itself can be asserted.
    private func runner(printing output: String, exiting code: Int) throws -> SettingsCommandRunner {
        let script = directory.appendingPathComponent("omnivia")
        let log = directory.appendingPathComponent("arguments").path
        // The document is written to a file and `cat`-ed rather than embedded in
        // the script: a result document is multi-line JSON, and a here-document
        // carrying it is one quoting mistake away from a script that hangs.
        let document = directory.appendingPathComponent("stdout.json")
        try output.write(to: document, atomically: true, encoding: .utf8)
        try """
        #!/bin/sh
        for argument in "$@"; do printf '%s\\n' "$argument" >> '\(log)'; done
        cat '\(document.path)'
        exit \(code)
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: script.path
        )
        return SettingsCommandRunner(
            executable: script,
            installationState: directory.appendingPathComponent("state"),
            workspaceID: "ws-01HQ"
        )
    }

    private func recordedArguments() throws -> [String] {
        let text = try String(
            contentsOf: directory.appendingPathComponent("arguments"), encoding: .utf8
        )
        return text.split(separator: "\n").map(String.init)
    }

    private static let oneSetting = """
    {
      "settings": [
        {
          "key": "backup.retention_days",
          "value_json": "30",
          "record_version": "4",
          "updated_at": "2026-10-18T02:14:09.120Z",
          "updated_by": "omnivia-core-service"
        }
      ]
    }
    """

    // MARK: What is asked

    /// Every settings call names its workspace. Without it the CLI refuses, and
    /// a call that reached a service under a different grant would be worse.
    func testEveryCallNamesTheWorkspaceAndTheInstallationState() async throws {
        let runner = try runner(printing: Self.oneSetting, exiting: 0)
        _ = await runner.list(prefix: nil)
        let arguments = try recordedArguments()
        XCTAssertEqual(arguments.first, "--installation-state")
        XCTAssertTrue(arguments.contains("--workspace-id"))
        XCTAssertTrue(arguments.contains("ws-01HQ"))
        XCTAssertEqual(Array(arguments[4..<6]), ["settings", "list"])
    }

    /// An absent prefix is `{}`, not `{"prefix":""}`: the contract's field is
    /// optional, and an empty string is a value rather than its absence.
    func testAnAbsentPrefixSendsAnEmptyInputDocument() async throws {
        let runner = try runner(printing: Self.oneSetting, exiting: 0)
        _ = await runner.list(prefix: nil)
        XCTAssertTrue(try recordedArguments().contains("{}"))
    }

    /// A namespace is named without a trailing dot: the contract's key grammar
    /// admits none, and the service matches on the boundary.
    func testAPrefixIsSentAsTheContractsField() async throws {
        let runner = try runner(printing: Self.oneSetting, exiting: 0)
        _ = await runner.list(prefix: "backup")
        XCTAssertTrue(try recordedArguments().contains("{\"prefix\":\"backup\"}"))
    }

    /// The mutation always carries the key it was given. A retry with the same
    /// key is a replay; a fresh one would be a second write.
    func testSetSendsItsIdempotencyKey() async throws {
        let runner = try runner(
            printing: #"{"setting":{"key":"a.b","value_json":"1","record_version":"1","updated_at":"2026-10-18T02:14:09.120Z","updated_by":"omnivia-core-service"}}"#,
            exiting: 0
        )
        _ = await runner.set(key: "a.b", valueJSON: "1", idempotencyKey: "idem-7")
        let arguments = try recordedArguments()
        XCTAssertTrue(arguments.contains("--idempotency-key"))
        XCTAssertTrue(arguments.contains("idem-7"))
    }

    /// A value is arbitrary JSON text, so it is encoded rather than
    /// interpolated: a quote inside it must not end the document early.
    func testAValueContainingQuotesIsEncodedNotInterpolated() {
        let input = SettingsCommandRunner.setInput(
            key: "capture.destination", valueJSON: #"{"path":"/Users/a \"b\"/Docs"}"#
        )
        let decoded = try? JSONSerialization.jsonObject(with: Data(input.utf8))
        let mapping = decoded as? [String: String]
        XCTAssertEqual(mapping?["key"], "capture.destination")
        XCTAssertEqual(mapping?["value_json"], #"{"path":"/Users/a \"b\"/Docs"}"#)
    }

    // MARK: What comes back

    func testASettingIsDecodedWhole() async throws {
        let runner = try runner(printing: Self.oneSetting, exiting: 0)
        let records = try await runner.list(prefix: nil).get()
        XCTAssertEqual(records.count, 1)
        XCTAssertEqual(records.first?.key, "backup.retention_days")
        XCTAssertEqual(records.first?.valueJSON, "30")
        XCTAssertEqual(records.first?.recordVersion, "4")
        XCTAssertEqual(records.first?.updatedBy, "omnivia-core-service")
    }

    /// No stored settings is an answer, not an error: a key never written has
    /// no row, and the caller applies its own default.
    func testAnEmptyListIsASuccessfulAnswer() async throws {
        let runner = try runner(printing: #"{"settings":[]}"#, exiting: 0)
        let records = try await runner.list(prefix: nil).get()
        XCTAssertEqual(records.count, 0)
    }

    /// A row short of a field makes the whole answer unreadable. Dropping it
    /// would render a partial list as a complete one.
    func testARowMissingAFieldMakesTheAnswerUnreadable() async throws {
        let runner = try runner(
            printing: #"{"settings":[{"key":"a.b","value_json":"1"}]}"#, exiting: 0
        )
        let result = await runner.list(prefix: nil)
        XCTAssertEqual(result.failure, .unreadableOutput)
    }

    func testSetReturnsTheStoredSetting() async throws {
        let runner = try runner(
            printing: #"{"setting":{"key":"a.b","value_json":"true","record_version":"2","updated_at":"2026-10-18T02:14:09.120Z","updated_by":"omnivia-core-service"}}"#,
            exiting: 0
        )
        let record = try await runner.set(
            key: "a.b", valueJSON: "true", idempotencyKey: "idem-1"
        ).get()
        XCTAssertEqual(record.valueJSON, "true")
        XCTAssertEqual(record.recordVersion, "2")
    }

    /// The CLI prints the refusal on stderr and nothing on stdout, so a
    /// non-zero exit is the whole signal available here.
    func testANonZeroExitIsARefusal() async throws {
        let runner = try runner(printing: "", exiting: 1)
        let result = await runner.set(key: "a.b", valueJSON: "1", idempotencyKey: "k")
        XCTAssertEqual(result.failure, .refused)
    }

    func testAMissingCommandIsUnavailable() async {
        let runner = SettingsCommandRunner(
            executable: directory.appendingPathComponent("absent"),
            installationState: directory.appendingPathComponent("state"),
            workspaceID: "ws-01HQ"
        )
        let result = await runner.list(prefix: nil)
        XCTAssertEqual(result.failure, .unavailable)
    }
}

extension Result {
    fileprivate var failure: Failure? {
        if case .failure(let error) = self { return error }
        return nil
    }
}
