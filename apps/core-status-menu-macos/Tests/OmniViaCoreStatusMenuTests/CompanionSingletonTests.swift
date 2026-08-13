import Darwin
import Foundation
import XCTest
@testable import OmniViaCoreStatusMenu

final class CompanionSingletonTests: XCTestCase {
    private func temporaryInstallation() throws -> URL {
        let suffix = UUID().uuidString.prefix(8)
        let url = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("ovc-\(suffix)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    func testSecondLaunchForwardsOneFixedRefreshIntent() throws {
        let installation = try temporaryInstallation()
        let first = try CompanionSingleton.acquire(installationState: installation)
        guard case let .primary(primary) = first else {
            return XCTFail("the first launch must own the singleton")
        }
        let activation = expectation(description: "fixed refresh intent received")
        primary.startListening { activation.fulfill() }

        let second = try CompanionSingleton.acquire(installationState: installation)
        guard case .forwarded = second else {
            return XCTFail("the second launch must forward and exit")
        }
        wait(for: [activation], timeout: 2)
        primary.close()
    }

    func testCloseReleasesLockAndRemovesActivationEndpoint() throws {
        let installation = try temporaryInstallation()
        guard case let .primary(primary) = try CompanionSingleton.acquire(
            installationState: installation
        ) else {
            return XCTFail("the first launch must own the singleton")
        }
        primary.close()
        guard case let .primary(replacement) = try CompanionSingleton.acquire(
            installationState: installation
        ) else {
            return XCTFail("a later launch must acquire after clean close")
        }
        replacement.close()
    }

    func testCompanionStateAndActivationEndpointAreOwnerOnly() throws {
        let installation = try temporaryInstallation()
        guard case let .primary(primary) = try CompanionSingleton.acquire(
            installationState: installation
        ) else {
            return XCTFail("the first launch must own the singleton")
        }
        let directory = installation.appendingPathComponent("companion")
        let socket = directory.appendingPathComponent("status-menu.sock")
        let directoryMode = try FileManager.default.attributesOfItem(atPath: directory.path)[.posixPermissions] as? NSNumber
        let socketMode = try FileManager.default.attributesOfItem(atPath: socket.path)[.posixPermissions] as? NSNumber
        XCTAssertEqual(directoryMode?.intValue, 0o700)
        XCTAssertEqual(socketMode?.intValue, 0o600)
        primary.close()
    }
}
