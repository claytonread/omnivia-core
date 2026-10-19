import Foundation
import XCTest
@testable import OmniViaCoreStatusMenu

final class CLIResolutionTests: XCTestCase {
    func testExplicitCLIOverrideWins() throws {
        let expected = "/reviewed/bin/omnivia"
        let resolver = CLIResolver(isExecutable: { $0 == expected })

        let result = try resolver.resolve(
            override: URL(fileURLWithPath: expected),
            bundleURL: URL(fileURLWithPath: "/App.app"),
            companionExecutableURL: URL(fileURLWithPath: "/App.app/Contents/MacOS/menu"),
            pathEnvironment: "/elsewhere"
        )

        XCTAssertEqual(result.path, expected)
    }

    func testBundledResourceWinsBeforeCommonLocationsAndPath() throws {
        let bundled = "/App.app/Contents/Resources/omnivia"
        let executables = Set([
            bundled,
            "/opt/homebrew/bin/omnivia",
            "/path/bin/omnivia",
        ])
        let resolver = CLIResolver(isExecutable: { executables.contains($0) })

        let result = try resolver.resolve(
            override: nil,
            bundleURL: URL(fileURLWithPath: "/App.app"),
            companionExecutableURL: URL(fileURLWithPath: "/App.app/Contents/MacOS/menu"),
            pathEnvironment: "/path/bin"
        )

        XCTAssertEqual(result.path, bundled)
    }

    func testFixedNameFallsBackToPath() throws {
        let expected = "/reviewed/path/omnivia"
        let resolver = CLIResolver(isExecutable: { $0 == expected })

        let result = try resolver.resolve(
            override: nil,
            bundleURL: nil,
            companionExecutableURL: nil,
            pathEnvironment: "/reviewed/path"
        )

        XCTAssertEqual(result.path, expected)
    }

    func testConfigurationRequiresExplicitAbsoluteOverrides() {
        XCTAssertThrowsError(
            try CompanionConfiguration.parse(
                arguments: ["menu", "--installation-state", "relative", "--workspace-id", "ws-1"]
            )
        )
        XCTAssertThrowsError(
            try CompanionConfiguration.parse(arguments: ["menu", "--cli"])
        )
    }

    func testInvocationNamesTheExplicitTargetBeforeTheNamespacedLifecycleCommand() {
        let invocation = CLIInvocation.lifecycle(
            executable: URL(fileURLWithPath: "/bin/omnivia"),
            installationState: URL(fileURLWithPath: "/tmp/omnivia-home/installation-state"),
            workspaceID: "ws-1",
            action: .status
        )

        XCTAssertEqual(
            invocation.arguments,
            [
                "--installation-state", "/tmp/omnivia-home/installation-state",
                "--workspace-id", "ws-1", "service", "status", "--json",
            ]
        )
    }

    func testConfigurationRequiresAnExplicitInstallationAndWorkspace() {
        XCTAssertThrowsError(try CompanionConfiguration.parse(arguments: ["menu"]))
        XCTAssertThrowsError(
            try CompanionConfiguration.parse(
                arguments: ["menu", "--installation-state", "/tmp/state"]
            )
        )
    }

    func testManagedLaunchPersistsOwnerOnlyConfigurationForOSColdStart() throws {
        let state = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-launch-\(UUID().uuidString)", isDirectory: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: state) }
        let expected = try CompanionConfiguration.loadOrParse(
            arguments: [
                "menu",
                "--cli", "/opt/omnivia/bin/omnivia",
                "--installation-state", "/tmp/omnivia/installation-state",
                "--workspace-id", "workspace-1",
            ],
            stateDirectory: state
        )

        let loaded = try CompanionConfiguration.loadOrParse(
            arguments: ["menu"],
            stateDirectory: state
        )

        XCTAssertEqual(loaded, expected)
        let file = state.appendingPathComponent("launch-configuration-v1.json")
        let mode = try FileManager.default.attributesOfItem(atPath: file.path)[.posixPermissions]
            as? NSNumber
        XCTAssertEqual(mode?.intValue, 0o600)
    }
}
