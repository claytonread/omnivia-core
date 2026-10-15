// Decoding `omnivia … mcp status --json`.
//
// The fixtures below are real output, captured by running the CLI against a
// scratch installation state, not hand-written from the schema.

import XCTest

@testable import CoreSettingsMacOS

final class McpStatusDecodingTests: XCTestCase {
    /// Verbatim from `omnivia --installation-state <scratch> mcp status --json`
    /// with nothing configured and no service running. Note this is printed
    /// with exit status 1.
    private let unconfigured = """
    {"hosts": [{"advertised_tool_count": null, "authoring_intent": null, \
    "configuration": "absent", "credential": "absent", "grant": "unknown", \
    "host": "claude-code", "principal_id": null, "profile": null, \
    "service": "unreachable", "workspace_id": null}, \
    {"advertised_tool_count": null, "authoring_intent": null, \
    "configuration": "absent", "credential": "absent", "grant": "unknown", \
    "host": "codex", "principal_id": null, "profile": null, \
    "service": "unreachable", "workspace_id": null}], "mcp_status_version": 1}
    """

    /// The configured shape, from the CLI's own acceptance fixtures.
    private let configured = """
    {"hosts": [{"advertised_tool_count": 11, "authoring_intent": true, \
    "configuration": "present", "credential": "present", "grant": "active", \
    "host": "claude-code", "principal_id": "mcp-claude-code-abc", \
    "profile": "authoring", "service": "reachable", "workspace_id": "ws-1"}], \
    "mcp_status_version": 1}
    """

    func testDecodesTheUnconfiguredRoster() throws {
        let status = try XCTUnwrap(McpCommandRunner.decode(unconfigured))
        XCTAssertEqual(status.hosts.count, 2)
        XCTAssertEqual(status.hosts.map(\.host), ["claude-code", "codex"])
        XCTAssertFalse(status.serviceReachable)
        let first = status.hosts[0]
        XCTAssertEqual(first.configuration, .absent)
        XCTAssertEqual(first.grant, .unknown)
        XCTAssertNil(first.profile)
        XCTAssertNil(first.advertisedToolCount)
    }

    func testDecodesAConfiguredHost() throws {
        let status = try XCTUnwrap(McpCommandRunner.decode(configured))
        let host = try XCTUnwrap(status.hosts.first)
        XCTAssertEqual(host.grant, .active)
        XCTAssertEqual(host.profile, .authoring)
        XCTAssertEqual(host.workspaceID, "ws-1")
        XCTAssertEqual(host.advertisedToolCount, 11)
        XCTAssertTrue(status.serviceReachable)
    }

    /// `authoring` is the contribute level: it carries the
    /// `workspace_contributor` role and eleven tools against restricted's six.
    func testProfileMapsToTheDesignsAccessLevels() {
        XCTAssertEqual(McpHostStatus.Profile.restricted.accessLevel, "Read only")
        XCTAssertEqual(McpHostStatus.Profile.authoring.accessLevel, "Read and contribute")
    }

    /// There is no display name in the model, so the pane derives one.
    func testHostNamesAreHumanReadable() throws {
        let status = try XCTUnwrap(McpCommandRunner.decode(unconfigured))
        XCTAssertEqual(status.hosts.map(\.displayName), ["Claude Code", "Codex"])
    }

    // MARK: Refusals

    func testAnUnknownDocumentVersionIsRefused() {
        let future = unconfigured.replacingOccurrences(
            of: "\"mcp_status_version\": 1", with: "\"mcp_status_version\": 2"
        )
        XCTAssertNil(
            McpCommandRunner.decode(future),
            "a version this build has not been written against must not be read as if it were v1"
        )
    }

    func testAnUnknownEnumValueIsRefusedRatherThanDefaulted() {
        let odd = configured.replacingOccurrences(
            of: "\"grant\": \"active\"", with: "\"grant\": \"suspended\""
        )
        XCTAssertNil(
            McpCommandRunner.decode(odd),
            "an unrecognised grant must not fall back to a value that reads as healthy"
        )
    }

    func testAMissingFieldIsRefused() {
        let truncated = configured.replacingOccurrences(
            of: "\"service\": \"reachable\", ", with: ""
        )
        XCTAssertNil(McpCommandRunner.decode(truncated))
    }

    func testNonJsonIsRefused() {
        XCTAssertNil(McpCommandRunner.decode("the installation service could not be reached"))
        XCTAssertNil(McpCommandRunner.decode(""))
    }
}
