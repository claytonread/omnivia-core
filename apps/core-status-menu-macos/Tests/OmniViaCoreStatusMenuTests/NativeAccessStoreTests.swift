import Foundation
import XCTest
@testable import DocumentIntakeMacOS

private final class TestBookmarkCodec: SecurityScopedBookmarkCoding, @unchecked Sendable {
    private(set) var starts = 0
    private(set) var stops = 0
    var stale = false
    var permitsAccess = true

    func makeReadOnlyBookmark(for url: URL) throws -> Data {
        Data(url.path.utf8)
    }

    func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
        guard let path = String(data: data, encoding: .utf8) else {
            throw NativeAccessError.invalidState
        }
        return (URL(fileURLWithPath: path), stale)
    }

    func startAccessing(_ url: URL) -> Bool {
        starts += 1
        return permitsAccess
    }

    func stopAccessing(_ url: URL) {
        stops += 1
    }
}

final class NativeAccessStoreTests: XCTestCase {
    private func temporaryDirectory(_ name: String = "state") throws -> URL {
        let root = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("omnivia-native-access-(UUID().uuidString)", isDirectory: true)
        let directory = root.appendingPathComponent(name, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return directory
    }

    func testDirectGrantPersistsOwnerOnlyAndBalancesGenerationCheckedAccess() throws {
        let state = try temporaryDirectory()
        let original = state.deletingLastPathComponent().appendingPathComponent("original.txt")
        try Data("body".utf8).write(to: original)
        let codec = TestBookmarkCodec()
        let store = try NativeAccessStore(stateDirectory: state, codec: codec)

        let adopted = try store.adopt(
            accessRef: "owner-a",
            linkedDocumentID: "doc-a",
            selectedURL: original,
            route: .directFile
        )
        let contents = try store.withReadAccess(
            accessRef: "owner-a",
            expectedGeneration: 1
        ) { try String(contentsOf: $0, encoding: .utf8) }

        XCTAssertEqual(adopted.generation, 1)
        XCTAssertEqual(contents, "body")
        XCTAssertEqual(codec.starts, 1)
        XCTAssertEqual(codec.stops, 1)
        let directoryMode = try FileManager.default.attributesOfItem(atPath: state.path)[.posixPermissions] as? NSNumber
        let file = state.appendingPathComponent("native-access-v1.json")
        let fileMode = try FileManager.default.attributesOfItem(atPath: file.path)[.posixPermissions] as? NSNumber
        XCTAssertEqual(directoryMode?.intValue, 0o700)
        XCTAssertEqual(fileMode?.intValue, 0o600)

        let reopened = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        XCTAssertEqual(reopened.descriptors(), [adopted])
    }

    func testDisconnectRevokesBytesAndRelinkAdvancesTheGeneration() throws {
        let state = try temporaryDirectory()
        let original = state.deletingLastPathComponent().appendingPathComponent("original.txt")
        try Data("body".utf8).write(to: original)
        let store = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        _ = try store.adopt(
            accessRef: "owner-a",
            linkedDocumentID: "doc-a",
            selectedURL: original,
            route: .directFile
        )

        let disconnected = try store.disconnect(accessRef: "owner-a")
        XCTAssertEqual(disconnected.state, .disconnected)
        XCTAssertEqual(disconnected.generation, 1)
        XCTAssertThrowsError(
            try store.withReadAccess(accessRef: "owner-a", expectedGeneration: 1) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .disconnected) }

        let relinked = try store.adopt(
            accessRef: "owner-a",
            linkedDocumentID: "doc-a",
            selectedURL: original,
            route: .directFile
        )
        XCTAssertEqual(relinked.generation, 2)
        XCTAssertThrowsError(
            try store.withReadAccess(accessRef: "owner-a", expectedGeneration: 1) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .generationMismatch) }
    }

    func testDisconnectDocumentRevokesEveryRouteAndNoOtherDocument() throws {
        let state = try temporaryDirectory()
        let root = state.deletingLastPathComponent()
        try Data("b".utf8).write(to: root.appendingPathComponent("b.txt"))
        try Data("c".utf8).write(to: root.appendingPathComponent("c.txt"))
        let codec = TestBookmarkCodec()
        let store = try NativeAccessStore(stateDirectory: state, codec: codec)
        _ = try store.adopt(
            accessRef: "owner-a",
            linkedDocumentID: "doc-a",
            selectedURL: root,
            route: .rootMember(rootRef: "root-a", memberRef: "member-a", relativeComponents: ["a.txt"])
        )
        _ = try store.adopt(
            accessRef: "owner-b",
            linkedDocumentID: "doc-a",
            selectedURL: root.appendingPathComponent("b.txt"),
            route: .directFile
        )
        _ = try store.adopt(
            accessRef: "owner-c",
            linkedDocumentID: "doc-b",
            selectedURL: root.appendingPathComponent("c.txt"),
            route: .directFile
        )

        let disconnected = try store.disconnect(linkedDocumentID: "doc-a")

        XCTAssertEqual(disconnected.map(\.accessRef), ["owner-a", "owner-b"])
        XCTAssertEqual(
            store.descriptors().first { $0.accessRef == "owner-c" }?.state,
            .connected
        )
    }

    func testRootMemberResolutionRefusesSymlinkEscape() throws {
        let state = try temporaryDirectory()
        let root = state.deletingLastPathComponent().appendingPathComponent("root", isDirectory: true)
        let outside = state.deletingLastPathComponent().appendingPathComponent("outside", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(
            at: root.appendingPathComponent("escape"),
            withDestinationURL: outside
        )
        let store = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        _ = try store.adopt(
            accessRef: "owner-a",
            linkedDocumentID: "doc-a",
            selectedURL: root,
            route: .rootMember(
                rootRef: "root-a",
                memberRef: "member-a",
                relativeComponents: ["escape", "secret.txt"]
            )
        )

        XCTAssertThrowsError(
            try store.withReadAccess(accessRef: "owner-a", expectedGeneration: 1) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .memberOutsideRoot) }
    }

    func testRoutesAndPeerRequirementsFailClosed() throws {
        let state = try temporaryDirectory()
        let store = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        XCTAssertThrowsError(
            try store.adopt(
                accessRef: "owner-a",
                linkedDocumentID: "doc-a",
                selectedURL: state,
                route: .rootMember(
                    rootRef: "root-a",
                    memberRef: "member-a",
                    relativeComponents: ["..", "escape"]
                )
            )
        ) { XCTAssertEqual($0 as? NativeAccessError, .invalidRoute) }
        XCTAssertThrowsError(try NativePeerRequirement("")) {
            XCTAssertEqual($0 as? NativePeerRequirementError, .invalidRequirement)
        }
        XCTAssertNoThrow(
            try NativePeerRequirement(
                "anchor apple generic and identifier \"com.omnivia.core.status\""
            )
        )
    }
}
