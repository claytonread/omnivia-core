import DocumentIntakeMacOS
import Foundation
import XCTest

final class OwnedSnapshotStoreTests: XCTestCase {
    func testStagesDataAndFileAsOwnerOnlyCopies() throws {
        let root = try temporaryDirectory()
        let source = root.appendingPathComponent("source.txt")
        try Data("from file".utf8).write(to: source)
        let storeRoot = root.appendingPathComponent("snapshots", isDirectory: true)
        let store = try OwnedSnapshotStore(root: storeRoot)

        let staged = try store.stage([
            .file(source),
            .data(Data("from data".utf8), suggestedFilename: "typed.md"),
        ])

        XCTAssertEqual(staged.count, 2)
        XCTAssertEqual(try Data(contentsOf: staged[0]), Data("from file".utf8))
        XCTAssertEqual(try Data(contentsOf: staged[1]), Data("from data".utf8))
        XCTAssertEqual(try permissions(storeRoot), 0o700)
        XCTAssertEqual(try permissions(staged[0]), 0o600)
        XCTAssertEqual(try permissions(staged[1]), 0o600)

        store.remove(staged)
        XCTAssertFalse(FileManager.default.fileExists(atPath: staged[0].path))
    }

    func testRefusesTraversalAndMoreThanTenItems() throws {
        let root = try temporaryDirectory()
        let store = try OwnedSnapshotStore(root: root.appendingPathComponent("snapshots"))
        XCTAssertThrowsError(try store.stage([
            .data(Data(), suggestedFilename: "../"),
        ])) { error in
            XCTAssertEqual(error as? OwnedSnapshotStoreError, .invalidFilename)
        }
        XCTAssertThrowsError(try store.stage((0...10).map {
            .data(Data(), suggestedFilename: "\($0).txt")
        })) { error in
            XCTAssertEqual(error as? OwnedSnapshotStoreError, .tooManyItems)
        }
    }

    private func permissions(_ url: URL) throws -> Int {
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        return (attributes[.posixPermissions] as? NSNumber)?.intValue ?? -1
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
