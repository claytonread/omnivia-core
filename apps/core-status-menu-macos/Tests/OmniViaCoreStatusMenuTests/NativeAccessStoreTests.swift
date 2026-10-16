import Foundation
import XCTest
@testable import DocumentIntakeMacOS

private final class TestBookmarkCodec: SecurityScopedBookmarkCoding, @unchecked Sendable {
    private(set) var makes = 0
    private(set) var starts = 0
    private(set) var stops = 0
    var stale = false
    var permitsAccess = true

    func makeReadOnlyBookmark(for url: URL) throws -> Data {
        makes += 1
        return Data(url.path.utf8)
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
    private static let binding = NativeRootAccessBinding(
        .companion(workspaceRef: "workspace-1")
    )

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
        let rootGrant = try store.adoptRoot(
            rootRef: "root-a",
            ownerRef: "run-a",
            selectedURL: root,
            binding: Self.binding
        )
        _ = try store.adoptRootMember(
            accessRef: "owner-a",
            linkedDocumentID: "doc-a",
            rootRef: "root-a",
            memberRef: "member-a",
            relativeComponents: ["a.txt"],
            expectedRootGeneration: rootGrant.generation
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
        let rootGrant = try store.adoptRoot(
            rootRef: "root-a",
            ownerRef: "run-a",
            selectedURL: root,
            binding: Self.binding
        )
        _ = try store.adoptRootMember(
            accessRef: "owner-a",
            linkedDocumentID: "doc-a",
            rootRef: "root-a",
            memberRef: "member-a",
            relativeComponents: ["escape", "secret.txt"],
            expectedRootGeneration: rootGrant.generation
        )

        XCTAssertThrowsError(
            try store.withReadAccess(
                accessRef: "owner-a",
                expectedGeneration: 1,
                expectedRootBindingGeneration: rootGrant.generation
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .memberOutsideRoot) }
    }

    func testRootMembersShareOnePersistedBookmarkUntilLastOwnerAndMemberRelease() throws {
        let state = try temporaryDirectory()
        let root = state.deletingLastPathComponent().appendingPathComponent("root", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try Data("a".utf8).write(to: root.appendingPathComponent("a.txt"))
        try Data("b".utf8).write(to: root.appendingPathComponent("b.txt"))
        let codec = TestBookmarkCodec()
        let store = try NativeAccessStore(stateDirectory: state, codec: codec)

        let runGrant = try store.adoptRoot(
            rootRef: "root-a",
            ownerRef: "run-a",
            selectedURL: root,
            binding: Self.binding
        )
        let subscriptionGrant = try store.adoptRoot(
            rootRef: "root-a",
            ownerRef: "subscription-a",
            selectedURL: root,
            binding: Self.binding,
            authority: .watchAndMembers
        )
        let first = try store.adoptRootMember(
            accessRef: "access-a",
            linkedDocumentID: "doc-a",
            rootRef: "root-a",
            memberRef: "member-a",
            relativeComponents: ["a.txt"],
            expectedRootGeneration: runGrant.generation
        )
        let second = try store.adoptRootMember(
            accessRef: "access-b",
            linkedDocumentID: "doc-b",
            rootRef: "root-a",
            memberRef: "member-b",
            relativeComponents: ["b.txt"],
            expectedRootGeneration: runGrant.generation
        )

        XCTAssertEqual(codec.makes, 1)
        XCTAssertEqual(runGrant.generation, subscriptionGrant.generation)
        XCTAssertEqual(subscriptionGrant.ownerRefs, ["run-a", "subscription-a"])
        XCTAssertEqual(first.rootBindingGeneration, runGrant.generation)
        XCTAssertEqual(second.rootBindingGeneration, runGrant.generation)
        let text = try store.withReadAccess(
            accessRef: first.accessRef,
            expectedGeneration: first.generation,
            expectedRootBindingGeneration: runGrant.generation
        ) { try String(contentsOf: $0, encoding: .utf8) }
        XCTAssertEqual(text, "a")

        let stateData = try Data(contentsOf: state.appendingPathComponent("native-access-v1.json"))
        let document = try XCTUnwrap(JSONSerialization.jsonObject(with: stateData) as? [String: Any])
        let records = try XCTUnwrap(document["records"] as? [[String: Any]])
        let roots = try XCTUnwrap(document["roots"] as? [[String: Any]])
        XCTAssertTrue(records.allSatisfy { $0["bookmark"] == nil })
        XCTAssertEqual(roots.filter { $0["bookmark"] != nil }.count, 1)

        // Releasing an owner is not a diagnosis: the grant keeps the state it
        // was in while any route still holds it.
        XCTAssertEqual(
            try store.releaseRoot(rootRef: "root-a", ownerRef: "run-a").state,
            .provisional
        )
        XCTAssertEqual(
            try store.releaseRoot(rootRef: "root-a", ownerRef: "subscription-a").state,
            .provisional
        )
        _ = try store.disconnect(accessRef: first.accessRef)
        XCTAssertEqual(try store.disconnect(accessRef: second.accessRef).state, .disconnected)
        XCTAssertEqual(store.rootDescriptors().first?.state, .disconnected)
    }

    func testEmptyRootOwnerPersistsAcrossRestart() throws {
        let state = try temporaryDirectory()
        let root = state.deletingLastPathComponent().appendingPathComponent("empty", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let grant = try NativeAccessStore(
            stateDirectory: state,
            codec: TestBookmarkCodec()
        ).adoptRoot(
            rootRef: "root-empty",
            ownerRef: "subscription-empty",
            selectedURL: root,
            binding: Self.binding,
            authority: .watchAndMembers
        )

        let reopened = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        XCTAssertEqual(reopened.rootDescriptors(), [grant])
        XCTAssertNoThrow(
            try reopened.withRootReadAccess(
                rootRef: grant.rootRef,
                expectedGeneration: grant.generation
            ) { resolved in
                XCTAssertEqual(resolved, root.resolvingSymlinksInPath().standardizedFileURL)
            }
        )
    }

    func testLegacyPerMemberBookmarksMigrateToOneRootGrant() throws {
        let state = try temporaryDirectory()
        let root = state.deletingLastPathComponent().appendingPathComponent("legacy", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let bookmark = Data(root.path.utf8).base64EncodedString()
        let legacy: [String: Any] = [
            "schema": "omnivia.native-access.v1",
            "records": [
                [
                    "accessRef": "access-a", "linkedDocumentID": "doc-a", "generation": 1,
                    "state": "connected", "bookmark": bookmark,
                    "route": [
                        "kind": "root_member", "rootRef": "root-a", "memberRef": "member-a",
                        "relativeComponents": ["a.txt"],
                    ],
                ],
                [
                    "accessRef": "access-b", "linkedDocumentID": "doc-b", "generation": 1,
                    "state": "connected", "bookmark": bookmark,
                    "route": [
                        "kind": "root_member", "rootRef": "root-a", "memberRef": "member-b",
                        "relativeComponents": ["b.txt"],
                    ],
                ],
            ],
        ]
        let stateFile = state.appendingPathComponent("native-access-v1.json")
        try JSONSerialization.data(withJSONObject: legacy).write(to: stateFile)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateFile.path)

        let migrated = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        XCTAssertEqual(migrated.rootDescriptors().count, 1)
        XCTAssertEqual(Set(migrated.descriptors().compactMap(\.rootBindingGeneration)), [1])
        // An upgrade keeps exactly the members it already had and gains no
        // right to discover anything new.
        let group = try XCTUnwrap(migrated.rootDescriptors().first)
        XCTAssertEqual(group.authority, .frozenMembersOnly)
        XCTAssertTrue(group.membersFrozen)
        XCTAssertEqual(group.memberRefs, ["member-a", "member-b"])
        XCTAssertNil(group.binding)
        XCTAssertThrowsError(
            try migrated.adoptRootMember(
                accessRef: "access-c",
                linkedDocumentID: "doc-c",
                rootRef: "root-a",
                memberRef: "member-c",
                relativeComponents: ["c.txt"],
                expectedRootGeneration: group.generation
            )
        ) { XCTAssertEqual($0 as? NativeAccessError, .membershipFrozen) }
        let rewritten = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: stateFile)) as? [String: Any]
        )
        XCTAssertEqual(rewritten["schema"] as? String, "omnivia.native-access.v3")
        XCTAssertTrue(
            try XCTUnwrap(rewritten["records"] as? [[String: Any]])
                .allSatisfy { $0["bookmark"] == nil }
        )
        XCTAssertEqual(
            try XCTUnwrap(rewritten["roots"] as? [[String: Any]])
                .filter { $0["bookmark"] != nil }.count,
            1
        )
    }

    func testLegacyContradictoryRootBookmarksFailClosed() throws {
        let state = try temporaryDirectory()
        let legacy: [String: Any] = [
            "schema": "omnivia.native-access.v1",
            "records": ["one", "two"].enumerated().map { index, name in
                [
                    "accessRef": "access-\(name)", "linkedDocumentID": "doc-\(name)",
                    "generation": 1, "state": "connected",
                    "bookmark": Data("/private/tmp/root-\(index)".utf8).base64EncodedString(),
                    "route": [
                        "kind": "root_member", "rootRef": "root-a", "memberRef": "member-\(name)",
                        "relativeComponents": ["file-\(index).txt"],
                    ],
                ]
            },
        ]
        let stateFile = state.appendingPathComponent("native-access-v1.json")
        try JSONSerialization.data(withJSONObject: legacy).write(to: stateFile)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateFile.path)

        XCTAssertThrowsError(
            try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        ) { XCTAssertEqual($0 as? NativeAccessError, .invalidState) }
    }

    /// The whole round trip: a v1 store is upgraded, then the install is rolled
    /// back and the older reader meets what the newer one left behind. Every
    /// original grant keeps the exact reference, document, generation and route
    /// it was recorded under, the upgraded group acquires no watch consent, and
    /// a document this build does not fully understand is refused with its
    /// bytes untouched rather than read as a subset and rewritten.
    func testV1AccessUpgradeSurvivesADowngradeThatBlocksANewerDocument() throws {
        let state = try temporaryDirectory()
        let root = state.deletingLastPathComponent().appendingPathComponent("legacy", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try Data("a".utf8).write(to: root.appendingPathComponent("a.txt"))
        let original = state.deletingLastPathComponent().appendingPathComponent("original.txt")
        try Data("body".utf8).write(to: original)
        let stateFile = state.appendingPathComponent("native-access-v1.json")
        try JSONSerialization.data(withJSONObject: [
            "schema": "omnivia.native-access.v1",
            "records": [
                [
                    "accessRef": "access-a", "linkedDocumentID": "doc-a", "generation": 3,
                    "state": "connected",
                    "bookmark": Data(root.path.utf8).base64EncodedString(),
                    "route": [
                        "kind": "root_member", "rootRef": "root-a", "memberRef": "member-a",
                        "relativeComponents": ["a.txt"],
                    ],
                ],
                [
                    "accessRef": "access-file", "linkedDocumentID": "doc-file", "generation": 2,
                    "state": "connected",
                    "bookmark": Data(original.path.utf8).base64EncodedString(),
                    "route": ["kind": "direct_file"],
                ],
            ],
        ]).write(to: stateFile)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateFile.path)

        let upgraded = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        let grants = upgraded.descriptors()
        let group = try XCTUnwrap(upgraded.rootDescriptors().first)
        let upgradedBytes = try Data(contentsOf: stateFile)
        XCTAssertEqual(grants.map(\.accessRef), ["access-a", "access-file"])
        XCTAssertEqual(grants.map(\.linkedDocumentID), ["doc-a", "doc-file"])
        // The upgrade carries each request's own identity across: no grant is
        // re-minted under a fresh reference or a reset generation.
        XCTAssertEqual(grants.map(\.generation), [3, 2])
        XCTAssertEqual(grants.map(\.rootBindingGeneration), [1, nil])
        // Both predate the lifecycle, so both are the established work they
        // were: nothing a v1 file retained is demoted to a candidate.
        XCTAssertEqual(grants.map(\.lifecycle), [.active, .active])
        XCTAssertEqual(
            grants.map(\.route),
            [
                .rootMember(rootRef: "root-a", memberRef: "member-a", relativeComponents: ["a.txt"]),
                .directFile,
            ]
        )
        // Upgrading a retained manual route is not consent to watch its folder:
        // the migrated group holds no durable owner, cannot enumerate, and
        // cannot enrol a sibling that appears later.
        XCTAssertEqual(group.authority, .frozenMembersOnly)
        XCTAssertTrue(group.membersFrozen)
        XCTAssertEqual(group.ownerRefs, [])
        XCTAssertEqual(group.memberRefs, ["member-a"])
        XCTAssertThrowsError(
            try upgraded.withRootReadAccess(
                rootRef: "root-a",
                expectedGeneration: group.generation,
                purpose: .memberDiscovery
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .discoveryNotPermitted) }
        XCTAssertThrowsError(
            try upgraded.adoptRootMember(
                accessRef: "access-b",
                linkedDocumentID: "doc-b",
                rootRef: "root-a",
                memberRef: "member-b",
                relativeComponents: ["b.txt"],
                expectedRootGeneration: group.generation
            )
        ) { XCTAssertEqual($0 as? NativeAccessError, .membershipFrozen) }

        // Reopening an already upgraded store migrates nothing a second time.
        let reopened = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        XCTAssertEqual(reopened.descriptors(), grants)
        XCTAssertEqual(try Data(contentsOf: stateFile), upgradedBytes)

        // What a later full solution leaves behind, in both shapes it can take:
        // a section this build does not define under the schema name it does
        // know, and a schema name it does not know at all. Each is refused
        // whole, and neither is downgraded, stripped or rewritten on disk.
        var newer = try XCTUnwrap(
            JSONSerialization.jsonObject(with: upgradedBytes) as? [String: Any]
        )
        newer["watchConsents"] = [["rootRef": "root-a", "consentGeneration": 1]]
        let future: [String: Any] = [
            "schema": "omnivia.native-access.v4",
            "records": [[String: Any]](),
            "roots": [[String: Any]](),
        ]
        for document in [newer, future] {
            let bytes = try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
            try bytes.write(to: stateFile)
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: stateFile.path
            )
            XCTAssertThrowsError(
                try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
            ) { XCTAssertEqual($0 as? NativeAccessError, .invalidState) }
            XCTAssertEqual(try Data(contentsOf: stateFile), bytes)
        }

        // Rolled back to the upgraded document, the older reader still holds
        // exactly the grants the upgrade produced, and still no watch consent.
        try upgradedBytes.write(to: stateFile)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateFile.path)
        let downgraded = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        XCTAssertEqual(downgraded.descriptors(), grants)
        XCTAssertEqual(downgraded.rootDescriptors(), [group])
        XCTAssertEqual(
            try downgraded.withReadAccess(
                accessRef: "access-a",
                expectedGeneration: 3,
                expectedRootBindingGeneration: 1
            ) { try String(contentsOf: $0, encoding: .utf8) },
            "a"
        )
        XCTAssertThrowsError(
            try downgraded.withRootReadAccess(
                rootRef: "root-a",
                expectedGeneration: group.generation,
                purpose: .memberDiscovery
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .discoveryNotPermitted) }
    }

    func testRootAuthorityIsProvisionalUntilItsDurableOwnerIsEstablished() throws {
        let state = try temporaryDirectory()
        let root = state.deletingLastPathComponent().appendingPathComponent("root", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let store = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())

        let grant = try store.adoptRoot(
            rootRef: "root-a",
            ownerRef: "subscription-a",
            selectedURL: root,
            binding: Self.binding,
            authority: .watchAndMembers
        )

        // Bound to its destination and readable for the scope it was selected
        // for, but no durable record owns it yet — so it cannot enumerate.
        XCTAssertEqual(grant.state, .provisional)
        XCTAssertNoThrow(
            try store.withRootReadAccess(
                rootRef: "root-a",
                expectedGeneration: grant.generation
            ) { _ in () }
        )
        XCTAssertThrowsError(
            try store.withRootReadAccess(
                rootRef: "root-a",
                expectedGeneration: grant.generation,
                purpose: .memberDiscovery
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .discoveryNotPermitted) }
        XCTAssertThrowsError(
            try store.activateRoot(rootRef: "root-a", expectedGeneration: grant.generation + 1)
        ) { XCTAssertEqual($0 as? NativeAccessError, .generationMismatch) }

        let active = try store.activateRoot(rootRef: "root-a", expectedGeneration: grant.generation)
        XCTAssertEqual(active.state, .active)
        XCTAssertEqual(active.generation, grant.generation)
        XCTAssertEqual(
            try store.activateRoot(rootRef: "root-a", expectedGeneration: grant.generation).state,
            .active
        )
        XCTAssertNoThrow(
            try store.withRootReadAccess(
                rootRef: "root-a",
                expectedGeneration: grant.generation,
                purpose: .memberDiscovery
            ) { _ in () }
        )

        let reopened = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        XCTAssertEqual(reopened.rootDescriptors().first?.state, .active)
        // Only an explicit release with nothing left routing through it
        // disconnects, and a disconnected root is not activated again.
        XCTAssertEqual(
            try reopened.releaseRoot(rootRef: "root-a", ownerRef: "subscription-a").state,
            .disconnected
        )
        XCTAssertThrowsError(
            try reopened.activateRoot(rootRef: "root-a", expectedGeneration: grant.generation)
        ) { XCTAssertEqual($0 as? NativeAccessError, .disconnected) }
    }

    /// AT-49: an external broker call against a provisional binding. Design
    /// §7.2: "A provisional route is not an active linked-file capability. It
    /// may prepare only its own frozen capture intent … External broker
    /// Refresh/Open/Reveal requires an active route."
    ///
    /// The two halves have to hold together. Preparation must keep working
    /// while the group is provisional, because that is exactly when
    /// establishment reads and publishes its reviewed members — a blanket
    /// provisional refusal would break every folder addition. The three
    /// explicit user actions must be refused over the same record in the same
    /// state, and must start working once a durable owner is established.
    func testProvisionalRootMemberRefusesOriginalActionsButStillPrepares() throws {
        let state = try temporaryDirectory()
        let root = state.deletingLastPathComponent().appendingPathComponent("root", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try Data("body".utf8).write(to: root.appendingPathComponent("a.txt"))
        let store = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        let grant = try store.adoptRoot(
            rootRef: "root-a",
            ownerRef: "run-a",
            selectedURL: root,
            binding: Self.binding
        )
        XCTAssertEqual(grant.state, .provisional)
        let member = try store.adoptRootMember(
            accessRef: "access-a",
            linkedDocumentID: "doc-a",
            rootRef: "root-a",
            memberRef: "member-a",
            relativeComponents: ["a.txt"],
            expectedRootGeneration: grant.generation
        )

        // Refresh, Open original and Reveal in Finder all resolve the original
        // through this one record, so one refusal covers the three.
        XCTAssertThrowsError(
            try store.withReadAccess(
                accessRef: member.accessRef,
                expectedGeneration: member.generation,
                expectedRootBindingGeneration: grant.generation,
                purpose: .originalAction
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .routeNotActive) }

        // The same record, the same generations, the same provisional group:
        // only the purpose differs, and preparation is what this group is for.
        XCTAssertEqual(
            try store.withReadAccess(
                accessRef: member.accessRef,
                expectedGeneration: member.generation,
                expectedRootBindingGeneration: grant.generation
            ) { try Data(contentsOf: $0) },
            Data("body".utf8)
        )

        // A durable owner now exists, which is the whole of what the refusal
        // was waiting for.
        try store.activateRoot(rootRef: "root-a", expectedGeneration: grant.generation)
        XCTAssertEqual(
            try store.withReadAccess(
                accessRef: member.accessRef,
                expectedGeneration: member.generation,
                expectedRootBindingGeneration: grant.generation,
                purpose: .originalAction
            ) { try Data(contentsOf: $0) },
            Data("body".utf8)
        )
    }

    /// Design §7.2 for the route that has no root group standing between it and
    /// its original. A grant adopted for a candidate prepares that candidate and
    /// serves no explicit original action; the publication that gives it a
    /// durable owner promotes it; the promotion survives a restart; and a
    /// disconnected grant is still refused for its own reason, not this one.
    func testDirectGrantIsProvisionalUntilPublishedWorkOwnsItAndStaysActive() throws {
        let state = try temporaryDirectory()
        let original = state.deletingLastPathComponent().appendingPathComponent("original.txt")
        try Data("body".utf8).write(to: original)
        let store = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        let adopted = try store.adopt(
            accessRef: "access-a",
            linkedDocumentID: "doc-a",
            selectedURL: original,
            route: .directFile
        )
        XCTAssertEqual(adopted.lifecycle, .provisional)

        // Refresh, Open original and Reveal in Finder all resolve the original
        // through this one record, so one refusal covers the three.
        XCTAssertThrowsError(
            try store.withReadAccess(
                accessRef: "access-a",
                expectedGeneration: adopted.generation,
                purpose: .originalAction
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .routeNotActive) }
        // The same record, the same generation: only the purpose differs, and
        // preparing this candidate is what the grant was adopted for.
        XCTAssertEqual(
            try store.withReadAccess(
                accessRef: "access-a",
                expectedGeneration: adopted.generation
            ) { try Data(contentsOf: $0) },
            Data("body".utf8)
        )

        // A promotion names exactly the grants a committed batch is bound to. A
        // reference this owner does not hold cannot be the active route that
        // batch promises, so it is refused rather than passed over — and
        // refused before anything is written, so the grant named beside it is
        // not promoted either.
        XCTAssertThrowsError(
            try store.activateGrants(accessRefs: ["access-a", "access-b"])
        ) { XCTAssertEqual($0 as? NativeAccessError, .unknownAccess) }
        XCTAssertEqual(store.descriptors().map(\.lifecycle), [.provisional])

        try store.activateGrants(accessRefs: ["access-a"])
        XCTAssertEqual(store.descriptors().map(\.lifecycle), [.active])
        XCTAssertEqual(
            try store.withReadAccess(
                accessRef: "access-a",
                expectedGeneration: adopted.generation,
                purpose: .originalAction
            ) { try Data(contentsOf: $0) },
            Data("body".utf8)
        )

        // Published work still owns it after a restart, so the capability it
        // gained is not an in-memory one.
        let reopened = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        XCTAssertEqual(reopened.descriptors().map(\.lifecycle), [.active])
        XCTAssertEqual(
            try reopened.withReadAccess(
                accessRef: "access-a",
                expectedGeneration: adopted.generation,
                purpose: .originalAction
            ) { try Data(contentsOf: $0) },
            Data("body".utf8)
        )

        try reopened.disconnect(accessRef: "access-a")
        XCTAssertThrowsError(
            try reopened.withReadAccess(
                accessRef: "access-a",
                expectedGeneration: adopted.generation,
                purpose: .originalAction
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .disconnected) }

        // Re-granting the same reference is a repair of established work, not a
        // new candidate: it comes back with the lifecycle its publication gave
        // it rather than demoted to provisional.
        let relinked = try reopened.adopt(
            accessRef: "access-a",
            linkedDocumentID: "doc-a",
            selectedURL: original,
            route: .directFile
        )
        XCTAssertEqual(relinked.generation, 2)
        XCTAssertEqual(relinked.lifecycle, .active)
        XCTAssertEqual(
            try reopened.withReadAccess(
                accessRef: "access-a",
                expectedGeneration: 2,
                purpose: .originalAction
            ) { try Data(contentsOf: $0) },
            Data("body".utf8)
        )
    }

    /// A direct-file grant written before the lifecycle was recorded predates
    /// the distinction, so it comes back as the established work it was — with
    /// its bookmark bytes and generation untouched. A member record claiming a
    /// lifecycle of its own is not a document this build wrote, and fails
    /// closed rather than being reinterpreted.
    func testGrantsWithoutARecordedLifecycleMigrateAsActive() throws {
        let state = try temporaryDirectory()
        let original = state.deletingLastPathComponent().appendingPathComponent("original.txt")
        try Data("body".utf8).write(to: original)
        let stateFile = state.appendingPathComponent("native-access-v1.json")
        try JSONSerialization.data(withJSONObject: [
            "schema": "omnivia.native-access.v3",
            "records": [
                [
                    "accessRef": "access-file", "linkedDocumentID": "doc-file", "generation": 4,
                    "state": "connected",
                    "bookmark": Data(original.path.utf8).base64EncodedString(),
                    "route": ["kind": "direct_file"],
                ],
            ],
            "roots": [[String: Any]](),
        ]).write(to: stateFile)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateFile.path)

        let store = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        let grant = try XCTUnwrap(store.descriptors().first)
        XCTAssertEqual(grant.lifecycle, .active)
        XCTAssertEqual(grant.generation, 4)
        XCTAssertEqual(
            try store.withReadAccess(
                accessRef: "access-file",
                expectedGeneration: 4,
                purpose: .originalAction
            ) { try Data(contentsOf: $0) },
            Data("body".utf8)
        )

        let broken = try temporaryDirectory("broken")
        let brokenFile = broken.appendingPathComponent("native-access-v1.json")
        try JSONSerialization.data(withJSONObject: [
            "schema": "omnivia.native-access.v3",
            "records": [
                [
                    "accessRef": "access-a", "linkedDocumentID": "doc-a", "generation": 1,
                    "rootBindingGeneration": 1, "state": "connected",
                    "lifecycle": "provisional",
                    "route": [
                        "kind": "root_member", "rootRef": "root-a", "memberRef": "member-a",
                        "relativeComponents": ["a.txt"],
                    ],
                ],
            ],
            "roots": [
                [
                    "rootRef": "root-a", "generation": 1, "state": "active",
                    "ownerRefs": ["subscription-a"], "authority": "watch_and_members",
                    "memberRefs": ["member-a"], "membersFrozen": false,
                    "bookmark": Data(state.path.utf8).base64EncodedString(),
                ],
            ],
        ]).write(to: brokenFile)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: brokenFile.path)
        XCTAssertThrowsError(
            try NativeAccessStore(stateDirectory: broken, codec: TestBookmarkCodec())
        ) { XCTAssertEqual($0 as? NativeAccessError, .invalidState) }
    }

    func testStaleRootAccessBecomesRepairableAndIsRepairedInPlace() throws {
        let state = try temporaryDirectory()
        let root = state.deletingLastPathComponent().appendingPathComponent("root", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try Data("a".utf8).write(to: root.appendingPathComponent("a.txt"))
        let codec = TestBookmarkCodec()
        let store = try NativeAccessStore(stateDirectory: state, codec: codec)
        let grant = try store.adoptRoot(
            rootRef: "root-a",
            ownerRef: "run-a",
            selectedURL: root,
            binding: Self.binding
        )
        try store.activateRoot(rootRef: "root-a", expectedGeneration: grant.generation)
        let member = try store.adoptRootMember(
            accessRef: "access-a",
            linkedDocumentID: "doc-a",
            rootRef: "root-a",
            memberRef: "member-a",
            relativeComponents: ["a.txt"],
            expectedRootGeneration: grant.generation
        )

        codec.stale = true
        XCTAssertThrowsError(
            try store.withReadAccess(
                accessRef: member.accessRef,
                expectedGeneration: member.generation,
                expectedRootBindingGeneration: grant.generation
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .staleBookmark) }

        // A member resolves through the root's bookmark, so the condition is
        // recorded against the group — and the route it is bound to is kept.
        XCTAssertEqual(store.rootDescriptors().first?.state, .needsAccess)
        let roots = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: try Data(contentsOf: state.appendingPathComponent("native-access-v1.json"))
            ) as? [String: Any]
        )["roots"] as? [[String: Any]]
        XCTAssertEqual(try XCTUnwrap(roots).filter { $0["bookmark"] != nil }.count, 1)

        // From here reads are refused by the recorded condition rather than by
        // resolving again, and a restart does not collapse it into a state that
        // can no longer be repaired.
        codec.stale = false
        let starts = codec.starts
        XCTAssertThrowsError(
            try store.withRootReadAccess(
                rootRef: "root-a",
                expectedGeneration: grant.generation
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .staleBookmark) }
        XCTAssertEqual(codec.starts, starts)
        let reopened = try NativeAccessStore(stateDirectory: state, codec: codec)
        XCTAssertEqual(reopened.rootDescriptors().first?.state, .needsAccess)

        // Re-granting the same root repairs it in place: the grant identity and
        // its enrolled members survive, so every bound member route still reads.
        let repaired = try reopened.adoptRoot(
            rootRef: "root-a",
            ownerRef: "run-a",
            selectedURL: root,
            binding: Self.binding
        )
        XCTAssertEqual(repaired.generation, grant.generation)
        XCTAssertEqual(repaired.state, .active)
        XCTAssertEqual(repaired.ownerRefs, ["run-a"])
        XCTAssertEqual(repaired.memberRefs, ["member-a"])
        XCTAssertEqual(
            try reopened.withReadAccess(
                accessRef: member.accessRef,
                expectedGeneration: member.generation,
                expectedRootBindingGeneration: grant.generation
            ) { try String(contentsOf: $0, encoding: .utf8) },
            "a"
        )
    }

    func testRootThatCannotBeAccessedRightNowRefusesReadsUntilItIsRepaired() throws {
        let state = try temporaryDirectory()
        let root = state.deletingLastPathComponent().appendingPathComponent("root", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let codec = TestBookmarkCodec()
        let store = try NativeAccessStore(stateDirectory: state, codec: codec)
        let grant = try store.adoptRoot(
            rootRef: "root-a",
            ownerRef: "subscription-a",
            selectedURL: root,
            binding: Self.binding,
            authority: .watchAndMembers
        )
        try store.activateRoot(rootRef: "root-a", expectedGeneration: grant.generation)

        codec.permitsAccess = false
        XCTAssertThrowsError(
            try store.withRootReadAccess(
                rootRef: "root-a",
                expectedGeneration: grant.generation
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .accessDenied) }
        XCTAssertEqual(store.rootDescriptors().first?.state, .unavailable)

        codec.permitsAccess = true
        let starts = codec.starts
        XCTAssertThrowsError(
            try store.withRootReadAccess(
                rootRef: "root-a",
                expectedGeneration: grant.generation,
                purpose: .memberDiscovery
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .accessDenied) }
        XCTAssertEqual(codec.starts, starts)

        let repaired = try store.adoptRoot(
            rootRef: "root-a",
            ownerRef: "subscription-a",
            selectedURL: root,
            binding: Self.binding,
            authority: .watchAndMembers
        )
        XCTAssertEqual(repaired.state, .active)
        XCTAssertEqual(repaired.generation, grant.generation)
        XCTAssertNoThrow(
            try store.withRootReadAccess(
                rootRef: "root-a",
                expectedGeneration: grant.generation,
                purpose: .memberDiscovery
            ) { _ in () }
        )
    }

    func testPersistedConnectedRootMigratesToActiveAndInvariantsStayEnforced() throws {
        let state = try temporaryDirectory()
        let root = state.deletingLastPathComponent().appendingPathComponent("root", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try Data("a".utf8).write(to: root.appendingPathComponent("a.txt"))
        let stateFile = state.appendingPathComponent("native-access-v1.json")
        let legacy: [String: Any] = [
            "schema": "omnivia.native-access.v2",
            "records": [
                [
                    "accessRef": "access-a", "linkedDocumentID": "doc-a", "generation": 1,
                    "rootBindingGeneration": 1, "state": "connected",
                    "route": [
                        "kind": "root_member", "rootRef": "root-a", "memberRef": "member-a",
                        "relativeComponents": ["a.txt"],
                    ],
                ],
            ],
            "roots": [
                [
                    "rootRef": "root-a", "generation": 1, "state": "connected",
                    "ownerRefs": ["subscription-a"],
                    "bookmark": Data(root.path.utf8).base64EncodedString(),
                ],
            ],
        ]
        try JSONSerialization.data(withJSONObject: legacy).write(to: stateFile)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateFile.path)

        // A root persisted under the file grant's two states was established
        // work, so it comes back as `active` and keeps serving its members.
        let migrated = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        let group = try XCTUnwrap(migrated.rootDescriptors().first)
        XCTAssertEqual(group.state, .active)
        XCTAssertEqual(group.generation, 1)
        XCTAssertEqual(migrated.descriptors().map(\.lifecycle), [.active])
        XCTAssertEqual(
            try migrated.withReadAccess(
                accessRef: "access-a",
                expectedGeneration: 1,
                expectedRootBindingGeneration: 1
            ) { try String(contentsOf: $0, encoding: .utf8) },
            "a"
        )

        // The invariant is the route, not the label: a repairable state without
        // the bookmark it is meant to repair is refused.
        let broken = try temporaryDirectory("broken")
        let brokenFile = broken.appendingPathComponent("native-access-v1.json")
        try JSONSerialization.data(withJSONObject: [
            "schema": "omnivia.native-access.v3",
            "records": [[String: Any]](),
            "roots": [
                [
                    "rootRef": "root-a", "generation": 1, "state": "needs_access",
                    "ownerRefs": ["subscription-a"], "authority": "import_plan",
                    "memberRefs": [String](), "membersFrozen": false,
                ],
            ],
        ]).write(to: brokenFile)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: brokenFile.path
        )
        XCTAssertThrowsError(
            try NativeAccessStore(stateDirectory: broken, codec: TestBookmarkCodec())
        ) { XCTAssertEqual($0 as? NativeAccessError, .invalidState) }
    }

    /// The production path, over one real `DocumentIntakeEnvironment`: the
    /// submission adopts the grant, its own publication promotes it inside the
    /// publication critical section, the watch registered beside it names that
    /// same grant, and a restart finds it still owned.
    func testASubmittedOriginalIsPromotedByItsOwnPublicationAndSurvivesRestart() async throws {
        let state = try temporaryDirectory("intake")
        let original = state.deletingLastPathComponent().appendingPathComponent("invoice.txt")
        try Data("invoice".utf8).write(to: original)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: TestBookmarkCodec()
        )

        let entries = try await ManualCaptureSubmission(
            environment: environment,
            binding: .companion(workspaceRef: "workspace-1")
        ).submit([ManualCaptureCandidate(url: original, keepUpdated: true)])

        let accessRef = try XCTUnwrap(entries.first?.intent.originalBinding?.accessRecordId)
        let grant = try XCTUnwrap(
            environment.accessStore.descriptors().first { $0.accessRef == accessRef }
        )
        XCTAssertEqual(grant.lifecycle, .active)
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.accessRef), [accessRef])
        XCTAssertEqual(
            try environment.accessStore.withReadAccess(
                accessRef: accessRef,
                expectedGeneration: grant.generation,
                purpose: .originalAction
            ) { try Data(contentsOf: $0) },
            Data("invoice".utf8)
        )

        let restarted = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: TestBookmarkCodec()
        )
        XCTAssertEqual(
            restarted.accessStore.descriptors().first { $0.accessRef == accessRef }?.lifecycle,
            .active
        )
        XCTAssertEqual(
            try restarted.accessStore.withReadAccess(
                accessRef: accessRef,
                expectedGeneration: grant.generation,
                purpose: .originalAction
            ) { try Data(contentsOf: $0) },
            Data("invoice".utf8)
        )
    }

    /// The other side of the same boundary. A submission that publishes nothing
    /// promotes nothing, and neither does a candidate that is adopted and then
    /// discarded unsubmitted: both leave a provisional grant, which is released
    /// rather than left holding a capability it never earned.
    func testAFailedSubmissionAndADiscardedCandidateNeverBecomeActive() async throws {
        let state = try temporaryDirectory("intake")
        let root = state.deletingLastPathComponent()
        let alpha = root.appendingPathComponent("alpha.txt")
        let beta = root.appendingPathComponent("beta.txt")
        try Data("alpha".utf8).write(to: alpha)
        try Data("beta".utf8).write(to: beta)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: TestBookmarkCodec()
        )
        let submission = ManualCaptureSubmission(
            environment: environment,
            binding: .companion(workspaceRef: "workspace-1")
        )
        // One of the two reviewed files is gone, so the batch publishes none of
        // itself — after the other one's grant has already been adopted.
        try FileManager.default.removeItem(at: beta)

        do {
            _ = try await submission.submit([
                ManualCaptureCandidate(url: alpha, keepUpdated: true),
                ManualCaptureCandidate(url: beta, keepUpdated: true),
            ])
            XCTFail("the batch cannot publish")
        } catch {}

        XCTAssertEqual(environment.queue.allEntries(), [])
        XCTAssertEqual(environment.fileSubscriptions.all(), [])
        XCTAssertEqual(environment.accessStore.descriptors().map(\.state), [.disconnected])
        XCTAssertEqual(environment.accessStore.descriptors().map(\.lifecycle), [.provisional])

        let candidate = try submission.adoptOriginal(
            alpha,
            displayName: "alpha.txt",
            surface: .finderQuickAction
        )
        guard case let .adoptedOriginal(adoptedRef, generation, _) = candidate.source else {
            return XCTFail("an adopted original carries its own grant")
        }
        let adopted = try XCTUnwrap(
            environment.accessStore.descriptors().first { $0.accessRef == adoptedRef }
        )
        XCTAssertEqual(adopted.lifecycle, .provisional)
        XCTAssertThrowsError(
            try environment.accessStore.withReadAccess(
                accessRef: adoptedRef,
                expectedGeneration: generation,
                purpose: .originalAction
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .routeNotActive) }

        submission.releaseGrant(accessRef: adoptedRef)
        let released = try XCTUnwrap(
            environment.accessStore.descriptors().first { $0.accessRef == adoptedRef }
        )
        XCTAssertEqual(released.state, .disconnected)
        XCTAssertEqual(released.lifecycle, .provisional)
    }

    func testRoutesAndPeerRequirementsFailClosed() throws {
        let state = try temporaryDirectory()
        let store = try NativeAccessStore(stateDirectory: state, codec: TestBookmarkCodec())
        XCTAssertThrowsError(
            try store.adoptRootMember(
                accessRef: "owner-a",
                linkedDocumentID: "doc-a",
                rootRef: "root-a",
                memberRef: "member-a",
                relativeComponents: ["..", "escape"],
                expectedRootGeneration: 1
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
