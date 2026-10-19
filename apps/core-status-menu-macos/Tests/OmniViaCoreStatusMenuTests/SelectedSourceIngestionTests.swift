import CryptoKit
import Darwin
import DocumentIntakeMacOS
import Foundation
import XCTest
@testable import OmniViaCoreStatusMenu

/// AT-09, the native half: one selected original goes through the production
/// intake (`ManualCaptureSubmission`, the system bookmark codec and coordinated
/// reader) and the production delivery (`CaptureDeliveryCoordinator` over
/// `CLICaptureDelivery`) into a real Core, and is left exactly as it was found.
///
/// This test cannot stand a Core up itself. It runs only when
/// `test_at09_selected_source_end_to_end.py` supplies one, and that test reads the
/// retained copy back from the Core workspace and compares it to the same
/// recorded digest. Run by itself, it skips.
final class SelectedSourceIngestionTests: XCTestCase {
    private struct Original: Equatable {
        let bytes: Data
        let size: Int64
        let mode: UInt16
        let owner: UInt32
        let group: UInt32
        let inode: UInt64
        let hardLinks: UInt16
        let flags: UInt32
        let modified: [Int]
        let created: [Int]
        let changed: [Int]
        let extendedAttributes: [String: Data]
        let siblings: [String]
        let directoryModified: [Int]

        // `atime` is deliberately absent: reading a file is allowed to move it, and
        // nothing else about the file is.
        init(_ url: URL) throws {
            bytes = try Data(contentsOf: url)
            var info = stat()
            XCTAssertEqual(lstat(url.path, &info), 0)
            size = Int64(info.st_size)
            mode = info.st_mode
            owner = info.st_uid
            group = info.st_gid
            inode = info.st_ino
            hardLinks = info.st_nlink
            flags = info.st_flags
            modified = [info.st_mtimespec.tv_sec, info.st_mtimespec.tv_nsec]
            created = [info.st_birthtimespec.tv_sec, info.st_birthtimespec.tv_nsec]
            changed = [info.st_ctimespec.tv_sec, info.st_ctimespec.tv_nsec]
            extendedAttributes = Self.attributes(of: url.path)
            let directory = url.deletingLastPathComponent()
            siblings = try FileManager.default.contentsOfDirectory(atPath: directory.path).sorted()
            var parent = stat()
            XCTAssertEqual(lstat(directory.path, &parent), 0)
            directoryModified = [parent.st_mtimespec.tv_sec, parent.st_mtimespec.tv_nsec]
        }

        private static func attributes(of path: String) -> [String: Data] {
            let length = listxattr(path, nil, 0, XATTR_NOFOLLOW)
            guard length > 0 else { return [:] }
            var names = [CChar](repeating: 0, count: length)
            listxattr(path, &names, length, XATTR_NOFOLLOW)
            var result: [String: Data] = [:]
            for name in names.split(separator: 0).map({ String(cString: Array($0) + [0]) }) {
                let size = getxattr(path, name, nil, 0, 0, XATTR_NOFOLLOW)
                var value = [UInt8](repeating: 0, count: max(size, 0))
                getxattr(path, name, &value, value.count, 0, XATTR_NOFOLLOW)
                result[name] = Data(value)
            }
            return result
        }
    }

    func testSelectedOriginalReachesCoreByteForByteAndIsLeftUnchanged() async throws {
        let environment = ProcessInfo.processInfo.environment
        guard let fixture = environment["OMNIVIA_AT09_FIXTURE"],
              let digest = environment["OMNIVIA_AT09_FIXTURE_SHA256"],
              let cli = environment["OMNIVIA_AT09_CLI"],
              let installationState = environment["OMNIVIA_AT09_INSTALLATION_STATE"],
              let workspace = environment["OMNIVIA_AT09_WORKSPACE_ID"],
              let report = environment["OMNIVIA_AT09_REPORT"]
        else { throw XCTSkip("AT-09 needs the Core that test_at09_selected_source_end_to_end.py stands up") }

        // The person's file: the committed fixture, at a path of its own, carrying
        // attributes that a copy made "now" would not have by accident.
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("at09-\(UUID().uuidString)", isDirectory: true)
        let documents = root.appendingPathComponent("Documents", isDirectory: true)
        try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let selected = documents.appendingPathComponent("utf8-bom-mixed-eol.txt")
        let expected = try Data(contentsOf: URL(fileURLWithPath: fixture))
        XCTAssertEqual(Self.sha256(expected), digest, "the fixture is not the recorded one")
        try expected.write(to: selected)
        XCTAssertEqual(chmod(selected.path, 0o640), 0)
        XCTAssertEqual(setxattr(selected.path, "com.omnivia.at09", "kept", 4, 0, 0), 0)
        let old = timeval(tv_sec: 1_000_000_000, tv_usec: 123_456)
        var times = [old, old]
        XCTAssertEqual(utimes(selected.path, &times), 0)
        let before = try Original(selected)
        XCTAssertEqual(before.bytes, expected)

        let state = root.appendingPathComponent("state", isDirectory: true)
        let intake = try DocumentIntakeEnvironment(stateRoot: state)
        let binding = CaptureDeliveryBinding.installedCompanion(
            installationState: URL(fileURLWithPath: installationState),
            workspaceRef: workspace
        )
        let submission = ManualCaptureSubmission(environment: intake, binding: binding)
        let entries = try await submission.submit([ManualCaptureCandidate(url: selected)])
        let entry = try XCTUnwrap(entries.first)
        XCTAssertEqual(entries.count, 1)

        // What the companion froze for Core is the selected bytes, before any send.
        let frozen = try Data(contentsOf: intake.queue.payloadURL(for: entry.intent.itemId))
        XCTAssertEqual(frozen, expected)
        XCTAssertEqual(entry.intent.byteLength, UInt64(expected.count))
        XCTAssertEqual(entry.intent.localIntegrityDigest, "sha256:" + digest)

        let delivery = CLICaptureDelivery(
            executable: URL(fileURLWithPath: cli),
            installationState: URL(fileURLWithPath: installationState),
            deliveryBinding: binding
        )
        let coordinator = CaptureDeliveryCoordinator(queue: intake.queue, transport: delivery)
        let delivered = await coordinator.deliverPending()

        let admitted = try XCTUnwrap(delivered.first)
        XCTAssertEqual(delivered.count, 1)
        XCTAssertEqual(admitted.state, .admitted, "failure: \(admitted.failureCode ?? "none")")
        let captureID = try XCTUnwrap(admitted.admittedCaptureID)

        let after = try Original(selected)
        XCTAssertEqual(after, before, "ingestion changed the selected original")
        XCTAssertEqual(Self.sha256(after.bytes), digest)

        let linked = try XCTUnwrap(entry.intent.originalBinding?.linkedDocumentId)
        try JSONSerialization.data(
            withJSONObject: ["capture_id": captureID, "document_id": linked],
            options: [.sortedKeys]
        ).write(to: URL(fileURLWithPath: report))
    }

    private static func sha256(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
