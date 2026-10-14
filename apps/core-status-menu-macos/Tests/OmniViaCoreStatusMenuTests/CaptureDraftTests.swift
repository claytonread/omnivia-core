import CoreCaptureMacOS
import DocumentIntakeMacOS
import Foundation
import XCTest

final class CaptureDraftTests: XCTestCase {
    private struct BookmarkCodec: SecurityScopedBookmarkCoding {
        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
        }
        func startAccessing(_ url: URL) -> Bool { true }
        func stopAccessing(_ url: URL) {}
    }

    func testSupportedFileBecomesSubmittable() throws {
        let root = try temporaryDirectory()
        let file = root.appendingPathComponent("brief.pdf")
        try Data("pdf".utf8).write(to: file)
        var draft = CaptureDraft()

        try draft.add([file])

        XCTAssertTrue(draft.canSubmitFiles)
        XCTAssertEqual(draft.fileCandidates.map(\.displayName), ["brief.pdf"])
    }

    func testOwnershipCleanupCoversFolderRowsAndNotPlainlyPickedOnes() throws {
        let root = try temporaryDirectory()
        let file = root.appendingPathComponent("brief.pdf")
        try Data("pdf".utf8).write(to: file)
        let folder = root.appendingPathComponent("Briefs", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let native = NativeCandidateKey(
            nativeRequestID: UUID().uuidString.lowercased(),
            candidateRef: "nic-1"
        )
        var draft = CaptureDraft()

        try draft.add([
            ManualCaptureCandidate(
                url: folder,
                surface: .finderQuickAction,
                source: .adoptedFolderRoot(rootRef: "root-1", generation: 3, ownerRef: "owner-1"),
                native: native
            ),
        ])
        try draft.add([file])

        // A native folder row is a folder, never a publishable file, and it
        // holds the grant that ownership cleanup has to release.
        XCTAssertEqual(draft.items.map(\.kind), [.folder, .file(.pdf)])
        XCTAssertEqual(draft.fileCandidates.map(\.displayName), ["brief.pdf"])
        XCTAssertTrue(draft.canSubmitFiles)
        XCTAssertEqual(draft.ownedCandidates.map(\.displayName), ["Briefs"])
        XCTAssertEqual(draft.ownedCandidates.map(\.native), [native])
        XCTAssertEqual(
            draft.ownedCandidates.map(\.source),
            [.adoptedFolderRoot(rootRef: "root-1", generation: 3, ownerRef: "owner-1")]
        )
        // The plainly picked file owns nothing of its own, so it has no
        // candidate to release.
        XCTAssertNil(draft.items[1].ownedCandidate)
    }

    func testAFolderRootSourceNeverClassifiesAsAFile() throws {
        let root = try temporaryDirectory()
        let file = root.appendingPathComponent("brief.pdf")
        try Data("pdf".utf8).write(to: file)
        var draft = CaptureDraft()

        // A root grant never names a file: refused rather than published as one.
        XCTAssertThrowsError(try draft.add([
            ManualCaptureCandidate(
                url: file,
                surface: .finderQuickAction,
                source: .adoptedFolderRoot(rootRef: "root-1", generation: 1, ownerRef: "owner-1")
            ),
        ]))
        XCTAssertEqual(draft.items, [])
    }

    func testExternalInvocationSurfaceSurvivesDraftClassification() throws {
        let root = try temporaryDirectory()
        let file = root.appendingPathComponent("brief.pdf")
        try Data("pdf".utf8).write(to: file)
        var draft = CaptureDraft()

        try draft.add([file], surface: .finderService)

        XCTAssertEqual(draft.fileCandidates.map(\.surface), [.finderService])
    }

    func testCapturedSnapshotProvenanceSurvivesDraftClassification() throws {
        let root = try temporaryDirectory()
        let file = root.appendingPathComponent("shared.pdf")
        try Data("pdf".utf8).write(to: file)
        var draft = CaptureDraft()

        try draft.add([file], surface: .share, source: .capturedSnapshot)

        XCTAssertTrue(draft.containsSnapshots)
        XCTAssertEqual(draft.fileCandidates.map(\.source), [.capturedSnapshot])
    }

    func testFolderRemainsVisibleAndRequiresSeparateReview() throws {
        let root = try temporaryDirectory()
        var draft = CaptureDraft()

        try draft.add([root])

        XCTAssertTrue(draft.containsFolders)
        XCTAssertFalse(draft.canSubmitFiles)
    }

    func testUnsupportedDuplicateAndBoundAreRefused() throws {
        let root = try temporaryDirectory()
        let unsupported = root.appendingPathComponent("archive.bin")
        try Data("x".utf8).write(to: unsupported)
        var draft = CaptureDraft()
        // A picked file that is not a supported document stays listed, and
        // nothing is published while it is listed.
        try draft.add([unsupported])
        XCTAssertEqual(draft.items.map(\.kind), [.unsupported])
        XCTAssertFalse(draft.canSubmitFiles)
        draft.remove(at: 0)

        let supported = root.appendingPathComponent("note.txt")
        try Data("x".utf8).write(to: supported)
        try draft.add([supported])
        XCTAssertThrowsError(try draft.add([supported]))

        var over = CaptureDraft()
        let files = try (0...10).map { index -> URL in
            let file = root.appendingPathComponent("\(index).txt")
            try Data("x".utf8).write(to: file)
            return file
        }
        XCTAssertThrowsError(try over.add(files))
    }

    func testManualSubmissionPublishesFrozenBytesToDurableQueue() async throws {
        let root = try temporaryDirectory()
        let state = root.appendingPathComponent("state", isDirectory: true)
        let file = root.appendingPathComponent("brief.txt")
        try Data("captured now".utf8).write(to: file)
        let submission = try ManualCaptureSubmission(
            stateRoot: state,
            workspaceRef: "workspace-1",
            bookmarkCodec: BookmarkCodec()
        )

        let entries = try await submission.submit([ManualCaptureCandidate(url: file)])

        XCTAssertEqual(entries.count, 1)
        XCTAssertEqual(entries[0].state, .pending)
        XCTAssertEqual(entries[0].intent.workspaceRef, "workspace-1")
        XCTAssertEqual(entries[0].intent.displayName, "brief.txt")
        let reloaded = try CaptureIntentQueue(
            root: state.appendingPathComponent("capture-queue", isDirectory: true)
        )
        XCTAssertEqual(reloaded.allEntries(), entries)
    }

    func testSnapshotSubmissionDoesNotInventOriginalBinding() async throws {
        let root = try temporaryDirectory()
        let state = root.appendingPathComponent("state", isDirectory: true)
        let file = root.appendingPathComponent("shared.txt")
        try Data("captured content".utf8).write(to: file)
        let submission = try ManualCaptureSubmission(
            stateRoot: state,
            workspaceRef: "workspace-1",
            bookmarkCodec: BookmarkCodec()
        )

        let entries = try await submission.submit([
            ManualCaptureCandidate(
                url: file,
                surface: .share,
                source: .capturedSnapshot
            ),
        ])

        XCTAssertNil(entries.first?.intent.originalBinding)
        XCTAssertEqual(entries.first?.intent.surface, .share)
    }

    func testSnapshotCannotBeKeptUpdated() async throws {
        let root = try temporaryDirectory()
        let state = root.appendingPathComponent("state", isDirectory: true)
        let file = root.appendingPathComponent("shared.txt")
        try Data("captured content".utf8).write(to: file)
        let submission = try ManualCaptureSubmission(
            stateRoot: state,
            workspaceRef: "workspace-1",
            bookmarkCodec: BookmarkCodec()
        )

        do {
            _ = try await submission.submit([
                ManualCaptureCandidate(
                    url: file,
                    surface: .shortcut,
                    keepUpdated: true,
                    source: .capturedSnapshot
                ),
            ])
            XCTFail("Expected snapshot monitoring to be rejected")
        } catch {
            XCTAssertEqual(error as? ManualCaptureSubmissionError, .snapshotCannotBeMonitored)
        }
    }

    func testPreAdoptedOriginalKeepsTheAcknowledgedGrantThroughSubmission() async throws {
        let root = try temporaryDirectory()
        let state = root.appendingPathComponent("state", isDirectory: true)
        let file = root.appendingPathComponent("finder.txt")
        try Data("selected in Finder".utf8).write(to: file)
        let submission = try ManualCaptureSubmission(
            stateRoot: state,
            workspaceRef: "workspace-1",
            bookmarkCodec: BookmarkCodec()
        )

        let candidate = try submission.adoptOriginal(
            file,
            displayName: "finder.txt",
            surface: .finderQuickAction
        )
        guard case let .adoptedOriginal(accessRef, generation, documentID) = candidate.source else {
            return XCTFail("Expected a companion-owned original")
        }

        let entries = try await submission.submit([candidate])

        XCTAssertEqual(entries.first?.intent.originalBinding?.accessRecordId, accessRef)
        XCTAssertEqual(entries.first?.intent.originalBinding?.bindingGeneration, generation)
        XCTAssertEqual(entries.first?.intent.originalBinding?.linkedDocumentId, documentID)
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
