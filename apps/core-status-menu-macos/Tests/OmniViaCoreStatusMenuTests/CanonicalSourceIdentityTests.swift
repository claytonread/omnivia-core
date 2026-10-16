@testable import DocumentIntakeMacOS
import Foundation
import XCTest

/// MON-21, design §21.8.1 and FR-16: what decides that two routes are over the
/// same source, and what must never decide it.
///
/// The companion-level overlap case — two watched roots and a direct route over
/// one file, sharing one source, one document and one capture — is proved in
/// `FolderCaptureSubmissionTests`. These cover the identity rule itself and the
/// edges of it: another workspace, a rename, a restart, a revoked route, and
/// every way macOS can decline to identify a file.
final class CanonicalSourceIdentityTests: XCTestCase {
    private struct BookmarkCodec: SecurityScopedBookmarkCoding {
        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
        }
        func startAccessing(_ url: URL) -> Bool { true }
        func stopAccessing(_ url: URL) {}
    }

    private final class TestClock: @unchecked Sendable {
        private let lock = NSLock()
        private var value: Int64 = 0

        func advance(_ delta: Int64) { lock.withLock { value += delta } }
        var reader: @Sendable () -> Int64 { { [self] in self.lock.withLock { self.value } } }
    }

    // MARK: - The identity rule

    /// The key is the volume's identity plus the file's resource identifier, so
    /// it survives a rename and a move within the volume, and two files with
    /// the same bytes never share it. A real macOS answer is used: this is the
    /// one claim the test suite cannot make with a stub.
    func testNativeIdentityFollowsTheFileNotItsNameOrItsBytes() throws {
        let root = try temporaryDirectory()
        let original = root.appendingPathComponent("brief.txt")
        let twin = root.appendingPathComponent("twin.txt")
        try Data("identical".utf8).write(to: original)
        try Data("identical".utf8).write(to: twin)

        let before = try XCTUnwrap(NativeFileIdentity.key(for: original))
        let twinKey = try XCTUnwrap(NativeFileIdentity.key(for: twin))
        XCTAssertNotEqual(before, twinKey)

        let renamed = root.appendingPathComponent("renamed.txt")
        try FileManager.default.moveItem(at: original, to: renamed)
        // Changed bytes under the new name: neither the name nor the content
        // was ever part of the key.
        try Data("entirely different, and longer".utf8).write(to: renamed)
        XCTAssertEqual(NativeFileIdentity.key(for: renamed), before)

        // A file that is not there is not identified, and is certainly not
        // identified as the one that used to have that name.
        XCTAssertNil(NativeFileIdentity.key(for: original))
        XCTAssertNil(NativeFileIdentity.key(for: root.appendingPathComponent("absent.txt")))
    }

    /// A rename and a restart both keep the canonical source: the key contains
    /// no name, and the record is durable.
    func testARenameAndARestartKeepTheCanonicalSource() throws {
        let root = try temporaryDirectory()
        let state = root.appendingPathComponent("canonical", isDirectory: true)
        let file = root.appendingPathComponent("brief.txt")
        try Data("content".utf8).write(to: file)

        let first = try CanonicalSourceRegistry(stateDirectory: state)
        let before = try first.resolve(url: file, workspaceRef: "workspace-1")
        XCTAssertTrue(before.isCanonical)

        let renamed = root.appendingPathComponent("renamed-and-rewritten.md")
        try FileManager.default.moveItem(at: file, to: renamed)
        try Data("rewritten".utf8).write(to: renamed)

        // A fresh registry over the same durable state: this is the restart.
        let restarted = try CanonicalSourceRegistry(stateDirectory: state)
        let after = try restarted.resolve(url: renamed, workspaceRef: "workspace-1")
        XCTAssertEqual(after, before)
        XCTAssertEqual(restarted.all().count, 1)
        // One source, not a second one minted for the new name.
        XCTAssertEqual(
            restarted.all().first?.requestedSourceIdentity,
            before.requestedSourceIdentity
        )
    }

    /// The workspace is part of the key, so the same file configured under
    /// another workspace is another source with another document.
    func testTheSameFileInAnotherWorkspaceIsAnotherSource() throws {
        let root = try temporaryDirectory()
        let file = root.appendingPathComponent("brief.txt")
        try Data("content".utf8).write(to: file)
        let registry = try CanonicalSourceRegistry(
            stateDirectory: root.appendingPathComponent("canonical", isDirectory: true)
        )

        let one = try registry.resolve(url: file, workspaceRef: "workspace-1")
        let two = try registry.resolve(url: file, workspaceRef: "workspace-2")

        XCTAssertNotEqual(one.requestedSourceIdentity, two.requestedSourceIdentity)
        XCTAssertNotEqual(one.linkedDocumentID, two.linkedDocumentID)
        XCTAssertEqual(registry.all().count, 2)
        // Each workspace's record names its own workspace and nothing else.
        XCTAssertEqual(
            Set(registry.all().map(\.workspaceRef)),
            ["workspace-1", "workspace-2"]
        )
        // Asking again returns what each workspace already has.
        XCTAssertEqual(try registry.resolve(url: file, workspaceRef: "workspace-1"), one)
        XCTAssertEqual(try registry.resolve(url: file, workspaceRef: "workspace-2"), two)
    }

    /// Two paths to one file inside one reviewed scope stay separate sources:
    /// every member of one submission owes its own durable row, grant and
    /// queued item, so the second claim on a native key is refused the
    /// canonical identities rather than handed them.
    func testASecondPathToOneFileInsideOneSubmissionIsNotGivenTheCanonicalIdentities() throws {
        let root = try temporaryDirectory()
        let file = root.appendingPathComponent("brief.txt")
        try Data("content".utf8).write(to: file)
        let registry = try CanonicalSourceRegistry(
            stateDirectory: root.appendingPathComponent("canonical", isDirectory: true)
        )

        let first = try registry.resolve(url: file, workspaceRef: "workspace-1")
        let key = try XCTUnwrap(first.nativeKey)
        let second = try registry.resolve(
            url: file,
            workspaceRef: "workspace-1",
            claimedNativeKeys: [key]
        )

        XCTAssertFalse(second.isCanonical)
        XCTAssertNotEqual(second.requestedSourceIdentity, first.requestedSourceIdentity)
        XCTAssertNotEqual(second.linkedDocumentID, first.linkedDocumentID)
        // Nothing of the second claim was recorded, so no later route is handed
        // its identities either.
        XCTAssertEqual(registry.all().count, 1)
        XCTAssertNil(registry.record(sourceIdentity: second.requestedSourceIdentity))
    }

    // MARK: - Failing closed

    /// A file macOS will not qualify is never merged onto anything. It is given
    /// identities of its own, those identities are recorded nowhere, and asking
    /// twice about the very same file gives two different answers — the only
    /// safe answer when sameness is unknown. Nothing falls back to the path or
    /// the bytes.
    func testAnUnqualifiedFileIsNeverMergedAndIsNeverRecorded() throws {
        let root = try temporaryDirectory()
        let file = root.appendingPathComponent("brief.txt")
        try Data("content".utf8).write(to: file)
        let registry = try CanonicalSourceRegistry(
            stateDirectory: root.appendingPathComponent("canonical", isDirectory: true),
            identity: { _ in nil }
        )

        let first = try registry.resolve(url: file, workspaceRef: "workspace-1")
        let second = try registry.resolve(url: file, workspaceRef: "workspace-1")

        XCTAssertFalse(first.isCanonical)
        XCTAssertFalse(second.isCanonical)
        XCTAssertNotEqual(first.requestedSourceIdentity, second.requestedSourceIdentity)
        XCTAssertNotEqual(first.linkedDocumentID, second.linkedDocumentID)
        XCTAssertTrue(registry.all().isEmpty)
        XCTAssertNil(registry.record(sourceIdentity: first.requestedSourceIdentity))
    }

    /// Two overlapping roots over one file that macOS will not identify: two
    /// sources, two documents and two captures. The identity is unknown, so the
    /// routes are kept apart rather than guessed together on their equal bytes.
    func testOverlappingRootsOverAnUnidentifiableFileAreNeverCoalesced() async throws {
        let outer = try temporaryDirectory()
        let inner = outer.appendingPathComponent("inner", isDirectory: true)
        try FileManager.default.createDirectory(at: inner, withIntermediateDirectories: true)
        try Data("content".utf8).write(to: inner.appendingPathComponent("brief.txt"))
        let environment = try DocumentIntakeEnvironment(
            stateRoot: outer.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec(),
            nativeIdentity: { _ in nil }
        )

        for scope in [outer, inner] {
            let watch = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
            _ = try await watch.submit(
                try watch.plan(
                    root: scope,
                    policy: FolderCapturePolicy(
                        mode: .keepUpdated,
                        recursive: true,
                        includeHidden: false
                    )
                )
            )
        }

        let memberships = environment.folderSubscriptions.all().flatMap(\.members)
        XCTAssertEqual(memberships.count, 2)
        XCTAssertEqual(Set(memberships.map(\.requestedSourceIdentity)).count, 2)
        XCTAssertEqual(Set(memberships.map(\.linkedDocumentID)).count, 2)
        XCTAssertEqual(environment.queue.allEntries().count, 2)
        XCTAssertTrue(environment.canonicalSources.all().isEmpty)
    }

    /// Unreadable, malformed and ambiguous durable state all refuse to open.
    /// The registry decides canonical sameness, so a state it cannot vouch for
    /// is never partially believed.
    func testUnreadableMalformedAndAmbiguousStateRefuseToOpen() throws {
        let root = try temporaryDirectory()
        let state = root.appendingPathComponent("canonical", isDirectory: true)
        let file = root.appendingPathComponent("brief.txt")
        try Data("content".utf8).write(to: file)
        let identities = try CanonicalSourceRegistry(stateDirectory: state)
            .resolve(url: file, workspaceRef: "workspace-1")
        let stateFile = state.appendingPathComponent("canonical-sources-v1.json")
        let good = try Data(contentsOf: stateFile)

        // Truncated.
        try good.prefix(good.count / 2).write(to: stateFile, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateFile.path)
        XCTAssertThrowsError(try CanonicalSourceRegistry(stateDirectory: state)) {
            XCTAssertEqual($0 as? CanonicalSourceRegistryError, .invalidState)
        }

        // Another schema's document, under this one's name.
        try Data(#"{"schema":"omnivia.canonical-sources.v2","sources":[]}"#.utf8)
            .write(to: stateFile, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateFile.path)
        XCTAssertThrowsError(try CanonicalSourceRegistry(stateDirectory: state)) {
            XCTAssertEqual($0 as? CanonicalSourceRegistryError, .invalidState)
        }

        // Ambiguous: one workspace and one native file claimed by two records,
        // so there is no answer to which source this file is.
        let record = CanonicalSourceRecord(
            workspaceRef: "workspace-1",
            nativeKey: try XCTUnwrap(identities.nativeKey),
            linkedDocumentID: identities.linkedDocumentID,
            requestedSourceIdentity: identities.requestedSourceIdentity
        )
        let twin = CanonicalSourceRecord(
            workspaceRef: record.workspaceRef,
            nativeKey: record.nativeKey,
            linkedDocumentID: "doc-" + UUID().uuidString.lowercased(),
            requestedSourceIdentity: "source-" + UUID().uuidString.lowercased()
        )
        try write([record, twin], to: stateFile)
        XCTAssertThrowsError(try CanonicalSourceRegistry(stateDirectory: state)) {
            XCTAssertEqual($0 as? CanonicalSourceRegistryError, .invalidState)
        }

        // A source identity that is not an identifier at all.
        try write(
            [
                CanonicalSourceRecord(
                    workspaceRef: record.workspaceRef,
                    nativeKey: record.nativeKey,
                    linkedDocumentID: record.linkedDocumentID,
                    requestedSourceIdentity: "source /../with separators"
                ),
            ],
            to: stateFile
        )
        XCTAssertThrowsError(try CanonicalSourceRegistry(stateDirectory: state)) {
            XCTAssertEqual($0 as? CanonicalSourceRegistryError, .invalidState)
        }

        // Readable by anyone but this owner is not this owner's state.
        try good.write(to: stateFile, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: stateFile.path)
        XCTAssertThrowsError(try CanonicalSourceRegistry(stateDirectory: state)) {
            XCTAssertEqual($0 as? CanonicalSourceRegistryError, .invalidState)
        }

        // The good state still opens, so the refusals above are about the
        // damage and not about the file.
        try good.write(to: stateFile, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateFile.path)
        XCTAssertEqual(
            try CanonicalSourceRegistry(stateDirectory: state).all().map(\.requestedSourceIdentity),
            [identities.requestedSourceIdentity]
        )
    }

    // MARK: - Routes

    /// Revoking one route leaves the others: the canonical source is not a
    /// shared lifetime. The direct route is disconnected, and the two folder
    /// memberships over the same file still read it and keep their identities.
    func testRevokingTheDirectRouteLeavesTheFolderRoutesIntact() async throws {
        let outer = try temporaryDirectory()
        let inner = outer.appendingPathComponent("inner", isDirectory: true)
        try FileManager.default.createDirectory(at: inner, withIntermediateDirectories: true)
        let shared = inner.appendingPathComponent("shared.txt")
        try Data("content".utf8).write(to: shared)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: outer.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec()
        )
        for scope in [outer, inner] {
            let watch = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
            _ = try await watch.submit(
                try watch.plan(
                    root: scope,
                    policy: FolderCapturePolicy(
                        mode: .keepUpdated,
                        recursive: true,
                        includeHidden: false
                    )
                )
            )
        }
        _ = try await ManualCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        ).submit([ManualCaptureCandidate(url: shared, keepUpdated: true)])
        let direct = try XCTUnwrap(environment.fileSubscriptions.all().first)
        let source = direct.requestedSourceIdentity

        // One route revoked, by access ref: this is the revocation of a single
        // route, not of the source.
        _ = try environment.accessStore.disconnect(accessRef: direct.accessRef)

        // The direct route cannot be read through any more.
        XCTAssertThrowsError(
            try environment.accessStore.withReadAccess(
                accessRef: direct.accessRef,
                expectedGeneration: direct.bindingGeneration
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .disconnected) }
        // Both folder memberships still name the one canonical source and still
        // read the file through their own grants.
        let subscriptions = environment.folderSubscriptions.all()
        XCTAssertEqual(subscriptions.map(\.state), [.waitingForCore, .waitingForCore])
        for subscription in subscriptions {
            let membership = try XCTUnwrap(
                subscription.members.first { $0.displayName.hasSuffix("shared.txt") }
            )
            XCTAssertEqual(membership.requestedSourceIdentity, source)
            XCTAssertEqual(
                try environment.accessStore.withReadAccess(
                    accessRef: membership.accessRef,
                    expectedGeneration: membership.membershipGeneration,
                    expectedRootBindingGeneration: subscription.rootBindingGeneration
                ) { try String(contentsOf: $0, encoding: .utf8) },
                "content"
            )
        }
        // The source itself outlives the route that was revoked.
        XCTAssertEqual(
            environment.canonicalSources.record(sourceIdentity: source)?.requestedSourceIdentity,
            source
        )
    }

    /// A direct watch's automatic refresh binds to the revision an overlapping
    /// folder route already queued for the same source, against the same head,
    /// instead of racing a second one. The watch is still recorded as having
    /// published for the epoch it read: its obligation is discharged by the
    /// capture that serves it.
    func testADirectWatchRefreshBindsToTheOverlappingFolderRoutesRevision() async throws {
        let root = try temporaryDirectory()
        let watched = root.appendingPathComponent("watched", isDirectory: true)
        try FileManager.default.createDirectory(at: watched, withIntermediateDirectories: true)
        let shared = watched.appendingPathComponent("shared.txt")
        try Data("baseline".utf8).write(to: shared)
        let clock = TestClock()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: BookmarkCodec(),
            monotonicClock: clock.reader
        )
        let folder = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        _ = try await folder.submit(
            try folder.plan(
                root: watched,
                policy: FolderCapturePolicy(
                    mode: .keepUpdated,
                    recursive: true,
                    includeHidden: false
                )
            )
        )
        _ = try await ManualCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        ).submit([ManualCaptureCandidate(url: shared, keepUpdated: true)])

        // One initial capture for both routes, admitted, so both are current at
        // the same head.
        let initial = environment.queue.allEntries()
        XCTAssertEqual(initial.count, 1)
        let captureID = "dcap-" + String(repeating: "a", count: 64)
        try admit(try XCTUnwrap(initial.first), captureID: captureID, queue: environment.queue)
        _ = await environment.folderMonitoring.reconcileAll()
        _ = await environment.fileMonitoring.reconcileAll()
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.active])
        XCTAssertEqual(
            environment.fileSubscriptions.all().first?.currentSuccessfulCaptureID,
            captureID
        )

        // The folder route reads the change first and queues the one revision.
        try Data("changed".utf8).write(to: shared)
        _ = await environment.folderMonitoring.reconcileAll()
        let revision = try XCTUnwrap(
            environment.queue.allEntries().first { $0.intent.itemId != initial.first?.intent.itemId }
        )
        XCTAssertEqual(revision.intent.basePrecondition, .expected(captureID))

        // The direct watch now reads the same change. It queues nothing, binds
        // to that revision, and is recorded as having published it.
        let subscriptionID = try XCTUnwrap(
            environment.fileSubscriptions.all().first?.subscriptionID
        )
        await environment.fileMonitoring.markDirty(subscriptionIDs: [subscriptionID])
        clock.advance(3_000)
        let refreshed = await environment.fileMonitoring.reconcileAll()

        XCTAssertEqual(refreshed.map(\.queued), [true])
        XCTAssertEqual(environment.queue.allEntries().count, 2)
        XCTAssertEqual(
            environment.fileSubscriptions.all().first?.lastQueuedItemID,
            revision.intent.itemId
        )
        XCTAssertEqual(
            environment.queue.allEntries()
                .filter { $0.intent.requestedSourceIdentity == revision.intent.requestedSourceIdentity }
                .count,
            2
        )
    }

    // MARK: - Helpers

    private func write(_ records: [CanonicalSourceRecord], to stateFile: URL) throws {
        struct Document: Encodable {
            let schema = "omnivia.canonical-sources.v1"
            let sources: [CanonicalSourceRecord]
        }
        try JSONEncoder().encode(Document(sources: records))
            .write(to: stateFile, options: [.atomic])
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: stateFile.path
        )
    }

    private func admit(
        _ entry: CaptureQueueEntry,
        captureID: String,
        queue: CaptureIntentQueue
    ) throws {
        let digest = "sha256:" + String(repeating: "c", count: 64)
        let staging = try queue.beginStage(
            itemID: entry.intent.itemId,
            requestDigest: digest,
            descriptorDigest: digest
        )
        _ = try queue.markAdmitted(
            itemID: entry.intent.itemId,
            attemptGeneration: staging.attemptGeneration,
            captureID: captureID
        )
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-canonical-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
