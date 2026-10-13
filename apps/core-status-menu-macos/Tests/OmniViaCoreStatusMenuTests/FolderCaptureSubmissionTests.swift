import DocumentIntakeMacOS
import Foundation
import XCTest

final class FolderCaptureSubmissionTests: XCTestCase {
    private struct BookmarkCodec: SecurityScopedBookmarkCoding {
        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
        }
        func startAccessing(_ url: URL) -> Bool { true }
        func stopAccessing(_ url: URL) {}
    }

    func testPlanAppliesRecursiveHiddenAndSupportedFormatScope() throws {
        let root = try temporaryDirectory()
        try Data("root".utf8).write(to: root.appendingPathComponent("root.txt"))
        try Data("hidden".utf8).write(to: root.appendingPathComponent(".hidden.txt"))
        try Data("unsupported".utf8).write(to: root.appendingPathComponent("archive.bin"))
        let nested = root.appendingPathComponent("nested", isDirectory: true)
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        try Data("nested".utf8).write(to: nested.appendingPathComponent("brief.pdf"))
        let submission = try FolderCaptureSubmission(
            stateRoot: root.appendingPathComponent("state", isDirectory: true),
            workspaceRef: "workspace-1",
            bookmarkCodec: BookmarkCodec()
        )

        let topLevel = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(
                mode: .importOnce,
                recursive: false,
                includeHidden: false
            )
        )
        let recursive = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(
                mode: .importOnce,
                recursive: true,
                includeHidden: true
            )
        )

        XCTAssertEqual(topLevel.members.map(\.displayName), ["root.txt"])
        XCTAssertEqual(topLevel.skippedHidden, 1)
        XCTAssertEqual(topLevel.skippedUnsupported, 1)
        XCTAssertEqual(
            recursive.members.map(\.displayName),
            [".hidden.txt", "nested/brief.pdf", "root.txt"]
        )
    }

    func testImportOncePublishesFolderLineageAndFrozenMembers() async throws {
        let root = try temporaryDirectory()
        try Data("first".utf8).write(to: root.appendingPathComponent("one.txt"))
        try Data("second".utf8).write(to: root.appendingPathComponent("two.md"))
        let state = root.appendingPathComponent("state", isDirectory: true)
        let submission = try FolderCaptureSubmission(
            stateRoot: state,
            workspaceRef: "workspace-1",
            bookmarkCodec: BookmarkCodec()
        )
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(
                mode: .importOnce,
                recursive: true,
                includeHidden: false
            )
        )

        let entries = try await submission.submit(plan)

        XCTAssertEqual(entries.count, 2)
        XCTAssertTrue(entries.allSatisfy { $0.intent.surface == .folderImport })
        XCTAssertTrue(entries.allSatisfy { $0.intent.intentKind == .folderCapture })
        XCTAssertTrue(entries.allSatisfy { $0.intent.bulkOrigin != nil })
        XCTAssertTrue(entries.allSatisfy { $0.intent.consent == nil })
        XCTAssertEqual(Set(entries.compactMap { $0.intent.originalBinding?.rootAccessGroupId }).count, 1)
    }

    func testKeepUpdatedBaselineFreezesStandingConsent() async throws {
        let root = try temporaryDirectory()
        try Data("watch me".utf8).write(to: root.appendingPathComponent("watch.txt"))
        let state = root.appendingPathComponent("state", isDirectory: true)
        let submission = try FolderCaptureSubmission(
            stateRoot: state,
            workspaceRef: "workspace-1",
            bookmarkCodec: BookmarkCodec()
        )
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(
                mode: .keepUpdated,
                recursive: true,
                includeHidden: false
            )
        )

        let entries = try await submission.submit(plan)

        XCTAssertEqual(entries.count, 1)
        XCTAssertEqual(entries[0].intent.surface, .automaticFolder)
        XCTAssertEqual(entries[0].intent.intentKind, .folderCapture)
        XCTAssertNotNil(entries[0].intent.consent)
        XCTAssertNotNil(entries[0].intent.bulkOrigin)
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-folder-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
