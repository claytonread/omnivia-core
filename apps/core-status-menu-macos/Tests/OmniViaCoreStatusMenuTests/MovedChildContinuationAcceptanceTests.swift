import AppKit
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS
import Foundation
import XCTest

/// MON-17, design §21.8.1 and the §21.8.2 "child moved outside its watched root"
/// row, end to end over the real stores rather than over hand-built projections.
///
/// One physical child carries two routes: a membership of a watched folder and a
/// direct keep-updated link, sharing the canonical source MON-21 established. The
/// child then leaves the root. The root-owned route must stop reading it, no
/// candidate of that root's may publish once it has gone, and the Files & Folders
/// window — built here from the environment's own stores and
/// `documentAccessRoutes()` — must say which of the children it no longer reads
/// are still read through a link of their own, and stop saying so when that link
/// is disconnected. A sibling with no link of its own is the control.
@MainActor
final class MovedChildContinuationAcceptanceTests: XCTestCase {
    /// A real security-scoped bookmark resolves the file it was made for wherever
    /// that file has since moved to. This stand-in is told where it went, so a
    /// move is a move and not a deletion.
    private final class RelocatingBookmarkCodec: SecurityScopedBookmarkCoding, @unchecked Sendable {
        private let lock = NSLock()
        private var moves: [String: String] = [:]

        func relocate(_ from: URL, to: URL) {
            lock.withLock { moves[from.path] = to.path }
        }

        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }

        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            let path = String(decoding: data, as: UTF8.self)
            return (URL(fileURLWithPath: lock.withLock { moves[path] ?? path }), false)
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

    func testAChildThatLeavesTheRootStopsBeingReadThroughItAndItsOwnLinkIsDisclosed() async throws {
        let root = try temporaryDirectory()
        let watched = root.appendingPathComponent("watched", isDirectory: true)
        let elsewhere = root.appendingPathComponent("elsewhere", isDirectory: true)
        for directory in [watched, elsewhere] {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        }
        let child = watched.appendingPathComponent("brief.txt")
        let sibling = watched.appendingPathComponent("sibling.txt")
        try Data("brief".utf8).write(to: child)
        try Data("sibling".utf8).write(to: sibling)
        let codec = RelocatingBookmarkCodec()
        let clock = TestClock()
        let environment = try DocumentIntakeEnvironment(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            bookmarkCodec: codec,
            monotonicClock: clock.reader
        )

        // 1. The watched folder, then a direct keep-updated link to the very same
        //    child, both through the production submission paths.
        let folder = FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
        _ = try await folder.submit(
            try folder.plan(
                root: watched,
                policy: FolderCapturePolicy(mode: .keepUpdated, recursive: true, includeHidden: false)
            )
        )
        _ = try await ManualCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        ).submit([ManualCaptureCandidate(url: child, keepUpdated: true)])

        // 2. One canonical source and one document for the child's two routes
        //    (MON-21), and two connected access descriptors that are not each
        //    other's.
        var subscription = try XCTUnwrap(environment.folderSubscriptions.all().first)
        let childMember = try XCTUnwrap(subscription.members.first { $0.displayName == "brief.txt" })
        let siblingMember = try XCTUnwrap(
            subscription.members.first { $0.displayName == "sibling.txt" }
        )
        let direct = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(direct.linkedDocumentID, childMember.linkedDocumentID)
        XCTAssertEqual(direct.requestedSourceIdentity, childMember.requestedSourceIdentity)
        XCTAssertNotEqual(direct.accessRef, childMember.accessRef)
        XCTAssertNotEqual(siblingMember.linkedDocumentID, childMember.linkedDocumentID)
        let routes = Dictionary(
            uniqueKeysWithValues: environment.documentAccessRoutes().map { ($0.accessRef, $0) }
        )
        XCTAssertEqual(routes[direct.accessRef]?.state, .connected)
        XCTAssertEqual(routes[direct.accessRef]?.route, .directFile)
        XCTAssertEqual(routes[childMember.accessRef]?.state, .connected)
        if case .rootMember = routes[childMember.accessRef]?.route {} else {
            XCTFail("the membership must be a root-mediated route")
        }

        // The baselines Core admits, so the watch and the direct link both reach
        // their working state and a later scan is reading, not establishing.
        for entry in environment.queue.allEntries() {
            try admit(entry, captureID: "dcap-" + String(repeating: "a", count: 64), queue: environment.queue)
        }
        _ = await environment.folderMonitoring.reconcileAll()
        _ = await environment.fileMonitoring.reconcileAll()
        XCTAssertEqual(environment.folderSubscriptions.all().map(\.state), [.active])
        XCTAssertEqual(environment.fileSubscriptions.all().map(\.state), [.active])

        // 3. The child changes, the scan reads it and freezes a complete
        //    candidate, and the child leaves the root before that candidate is
        //    committed. The stale root-mediated candidate must not publish.
        let moved = elsewhere.appendingPathComponent("brief.txt")
        try Data("brief, revised".utf8).write(to: child)
        let before = Set(environment.queue.allEntries().map(\.intent.itemId))
        environment.preparation.afterCandidatePrepared = {
            guard !FileManager.default.fileExists(atPath: moved.path) else { return }
            try? FileManager.default.moveItem(at: child, to: moved)
            codec.relocate(child, to: moved)
        }
        _ = await environment.folderMonitoring.reconcileAll()
        environment.preparation.afterCandidatePrepared = nil

        XCTAssertTrue(
            FileManager.default.fileExists(atPath: moved.path),
            "the race must have run"
        )
        XCTAssertEqual(
            Set(environment.queue.allEntries().map(\.intent.itemId)),
            before,
            "a root-mediated candidate frozen before the child left must not publish"
        )
        XCTAssertEqual(
            environment.folderSubscriptions.all().map(\.state),
            [.active],
            "one child leaving is that membership's condition, not the whole scope's"
        )
        XCTAssertNil(
            environment.folderSubscriptions.all().first?.members
                .first { $0.memberID == childMember.memberID }?.lastQueuedItemID,
            "nothing of the refused read is recorded against the membership"
        )

        // The root route itself now reads nothing: the locator walks real native
        // relationships and refuses to follow the child out of the grant.
        subscription = try XCTUnwrap(environment.folderSubscriptions.all().first)
        XCTAssertThrowsError(
            try environment.accessStore.withReadAccess(
                accessRef: childMember.accessRef,
                expectedGeneration: childMember.membershipGeneration,
                expectedRootBindingGeneration: subscription.rootBindingGeneration
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .memberOutsideRoot) }

        // The real reconciliation closes that membership and queues nothing for it.
        let reports = await environment.folderMonitoring.reconcileAll()
        let report = try XCTUnwrap(reports.first)
        XCTAssertEqual(report.absent, 1)
        XCTAssertEqual(report.queued, 0)
        subscription = try XCTUnwrap(environment.folderSubscriptions.all().first)
        XCTAssertEqual(
            subscription.members.first { $0.memberID == childMember.memberID }?.state,
            .absent
        )
        XCTAssertEqual(
            subscription.members.first { $0.memberID == siblingMember.memberID }?.state,
            .present
        )
        XCTAssertEqual(Set(environment.queue.allEntries().map(\.intent.itemId)), before)

        // 4. The window's own projection, over the environment's real stores.
        let window = makeWindow(environment: environment)
        window.reload()
        XCTAssertEqual(
            folderFacts(window)["No longer read through this folder"],
            "1 of 2. Still read through their own link (1): brief.txt",
            "the absent child continues through its own connected direct route"
        )

        // The direct route is the one still reading, and it reads the child where
        // it now lives.
        XCTAssertEqual(
            try environment.accessStore.withReadAccess(
                accessRef: direct.accessRef,
                expectedGeneration: direct.bindingGeneration
            ) { try String(contentsOf: $0, encoding: .utf8) },
            "brief, revised"
        )

        // 5a. Stopping the folder affects that membership only (design §21.8.3).
        //     The direct link still reads, still queues through the real watch
        //     path, and the disclosure still names it.
        _ = try await environment.folderMonitoring.stop(subscriptionID: subscription.subscriptionID)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [direct.subscriptionID])
        clock.advance(3_000)
        let refreshed = await environment.fileMonitoring.reconcileAll()

        XCTAssertEqual(refreshed.map(\.queued), [true])
        XCTAssertEqual(
            environment.documentAccessRoutes().first { $0.accessRef == direct.accessRef }?.state,
            .connected
        )
        window.reload()
        XCTAssertEqual(
            folderFacts(window)["No longer read through this folder"],
            "2 of 2. Still read through their own link (1): brief.txt",
            "a stopped watch reads no member, and only the child has a link of its own"
        )
        XCTAssertEqual(
            window.model.items(segment: .files).map(\.displayName),
            ["brief.txt"]
        )

        // 5b. Disconnecting the direct route ends the continuation, and the
        //     disclosure stops claiming one.
        _ = try await environment.disconnectSource(
            linkedDocumentID: direct.linkedDocumentID,
            fallback: .companion(workspaceRef: "workspace-1")
        )
        window.reload()

        XCTAssertEqual(
            folderFacts(window)["No longer read through this folder"],
            "2 of 2. None of them has a link of its own, so none is still being read."
        )
        XCTAssertTrue(window.model.items(segment: .files).isEmpty)
        XCTAssertThrowsError(
            try environment.accessStore.withReadAccess(
                accessRef: direct.accessRef,
                expectedGeneration: direct.bindingGeneration
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .disconnected) }
    }

    // MARK: - Helpers

    /// The window the companion opens, over this environment's own stores and
    /// closures. No window is made: `reload()` builds the projection, which is
    /// what this test reads.
    private func makeWindow(
        environment: DocumentIntakeEnvironment
    ) -> FolderManagementWindowController {
        FolderManagementWindowController(
            workspaceName: "workspace-1",
            folderStore: environment.folderSubscriptions,
            folderMonitoring: environment.folderMonitoring,
            fileStore: environment.fileSubscriptions,
            fileMonitoring: environment.fileMonitoring,
            bulkStore: environment.folderBulkRuns,
            bulkCoordinator: environment.folderBulkCoordinator,
            activityStore: environment.captureActivity,
            observeActivity: { try environment.recordCaptureActivity() },
            rootGroups: { environment.rootAccessGroups() },
            documentRoutes: { environment.documentAccessRoutes() }
        )
    }

    private func folderFacts(_ window: FolderManagementWindowController) -> [String: String] {
        let item = window.model.items(segment: .folders).first
        return Dictionary(
            uniqueKeysWithValues: window.model.detail(for: item).map { ($0.label, $0.value) }
        )
    }

    private func admit(
        _ entry: CaptureQueueEntry,
        captureID: String,
        queue: CaptureIntentQueue
    ) throws {
        let digest = "sha256:" + String(repeating: "b", count: 64)
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
            .appendingPathComponent("omnivia-moved-child-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
