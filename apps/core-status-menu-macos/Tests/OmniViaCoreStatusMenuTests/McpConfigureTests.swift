// What `mcp configure` hands back.
//
// The snippet is the configure's product, not a side note: the CLI writes the
// installation's own protected configuration but deliberately leaves the
// host's file alone, so what it prints is the step still to be done. These
// drive `McpCommandRunner` against a stand-in executable rather than a mock,
// so the pipe draining and exit-code reading are exercised too.

import Foundation
import XCTest

@testable import CoreSettingsMacOS

final class McpConfigureTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("mcp-configure-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    /// A shell script standing in for the installed `omnivia`.
    private func runner(printing output: String, exiting code: Int) throws -> McpCommandRunner {
        let script = directory.appendingPathComponent("omnivia")
        try """
        #!/bin/sh
        printf '%s' '\(output)'
        exit \(code)
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: script.path
        )
        return McpCommandRunner(
            executable: script,
            installationState: directory.appendingPathComponent("state")
        )
    }

    func testTheSnippetIsReturnedOnSuccess() async throws {
        let snippet = """
        {
          "mcpServers": {
            "omnivia-core": {
              "command": "omnivia-core-mcp"
            }
          }
        }
        """
        let runner = try runner(printing: snippet, exiting: 0)
        let result = await runner.configure(
            host: "claude-code", workspace: "ws-1", profile: "restricted"
        )
        XCTAssertEqual(try result.get(), snippet)
    }

    /// A configure that changed nothing still prints the snippet, so an empty
    /// one means the CLI did not meet its contract — not "nothing to show".
    func testAnEmptySnippetIsARefusal() async throws {
        let runner = try runner(printing: "", exiting: 0)
        let result = await runner.configure(
            host: "claude-code", workspace: "ws-1", profile: "restricted"
        )
        XCTAssertEqual(result.failure, .unreadableOutput)
    }

    func testExitThreeIsReportedAsNotAdministrator() async throws {
        let runner = try runner(printing: "", exiting: 3)
        let result = await runner.configure(
            host: "codex", workspace: "ws-1", profile: "authoring"
        )
        XCTAssertEqual(result.failure, .notAdministrator)
    }

    func testAnyOtherNonZeroExitIsAFailure() async throws {
        let runner = try runner(printing: "refused", exiting: 1)
        let result = await runner.configure(
            host: "codex", workspace: "ws-1", profile: "authoring"
        )
        XCTAssertEqual(result.failure, .failed)
    }

    /// There is no executable at this path at all.
    func testAMissingCommandIsUnavailable() async {
        let runner = McpCommandRunner(
            executable: directory.appendingPathComponent("absent"),
            installationState: directory
        )
        let result = await runner.configure(
            host: "codex", workspace: "ws-1", profile: "authoring"
        )
        XCTAssertEqual(result.failure, .unavailable)
    }
}

extension Result {
    fileprivate var failure: Failure? {
        if case .failure(let error) = self { return error }
        return nil
    }
}
