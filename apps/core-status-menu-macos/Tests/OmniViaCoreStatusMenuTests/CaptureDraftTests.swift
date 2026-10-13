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
        XCTAssertThrowsError(try draft.add([unsupported]))

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

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
