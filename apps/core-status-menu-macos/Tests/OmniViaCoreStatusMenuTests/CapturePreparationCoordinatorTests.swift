import Foundation
import XCTest
@testable import DocumentIntakeMacOS

private final class PreparationBookmarkCodec: SecurityScopedBookmarkCoding, @unchecked Sendable {
    func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
    func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
        (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
    }
    func startAccessing(_ url: URL) -> Bool { true }
    func stopAccessing(_ url: URL) {}
}

private final class TestOriginalReader: CoordinatedOriginalReading, @unchecked Sendable {
    private let onRead: @Sendable (URL) throws -> Data

    init(_ onRead: @escaping @Sendable (URL) throws -> Data) {
        self.onRead = onRead
    }

    func readStableBytes(from url: URL, maximumBytes: UInt64) throws -> Data {
        let data = try onRead(url)
        guard UInt64(data.count) <= maximumBytes else { throw CapturePreparationError.fileTooLarge }
        return data
    }
}

final class CapturePreparationCoordinatorTests: XCTestCase {
    private func temporaryRoot() throws -> URL {
        let root = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("omnivia-preparation-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return root
    }

    private func seed(item: String, access: String, generation: UInt64) -> CaptureIntentSeed {
        CaptureIntentSeed(
            submissionId: "submission-1",
            itemId: item,
            targetRef: "target-1",
            workspaceRef: "workspace-1",
            targetBindingRevision: "target-binding-1",
            callerContextBinding: CallerContextBinding(
                clientContextRef: "client-1",
                principalRef: "principal-1",
                contextRevision: "context-1"
            ),
            surface: .manual,
            contentKind: .pdf,
            payloadRef: "payload-" + item,
            requestedSourceIdentity: "source-" + item,
            operationKey: "operation-" + item,
            intentKind: .refresh,
            basePrecondition: .expected(nil),
            processingProfile: ProcessingProfileBinding(
                profileRef: "profile-1",
                revision: "profile-revision-1"
            ),
            originalBinding: OriginalBindingIntent(
                accessRecordId: access,
                bindingGeneration: generation,
                linkedDocumentId: "document-" + item
            ),
            createdAt: "2026-10-14T00:00:00Z",
            displayName: item + ".pdf"
        )
    }

    func testPublishesCompletePreparedSelectionWithFrozenBytes() async throws {
        let root = try temporaryRoot()
        let accessStore = try NativeAccessStore(
            stateDirectory: root.appendingPathComponent("access"),
            codec: PreparationBookmarkCodec()
        )
        let queue = try CaptureIntentQueue(root: root.appendingPathComponent("queue"))
        var items: [CapturePreparationItem] = []
        for index in 1...2 {
            let item = "item-\(index)"
            let file = root.appendingPathComponent(item + ".pdf")
            try Data("body-\(index)".utf8).write(to: file)
            let access = "access-\(index)"
            _ = try accessStore.adopt(
                accessRef: access,
                linkedDocumentID: "document-" + item,
                selectedURL: file,
                route: .directFile
            )
            items.append(
                CapturePreparationItem(
                    access: NativeAccessExpectation(accessRef: access, generation: 1),
                    seed: seed(item: item, access: access, generation: 1)
                )
            )
        }
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: TestOriginalReader { try Data(contentsOf: $0) }
        )

        let published = try await coordinator.prepareAndPublish(items)

        XCTAssertEqual(published.map { $0.intent.itemId }, ["item-1", "item-2"])
        XCTAssertEqual(try Data(contentsOf: queue.payloadURL(for: "item-1")), Data("body-1".utf8))
        XCTAssertEqual(queue.allEntries().count, 2)
    }

    func testGenerationChangeAfterReadsRejectsWholeBatch() async throws {
        let root = try temporaryRoot()
        let codec = PreparationBookmarkCodec()
        let accessStore = try NativeAccessStore(
            stateDirectory: root.appendingPathComponent("access"),
            codec: codec
        )
        let queue = try CaptureIntentQueue(root: root.appendingPathComponent("queue"))
        let file = root.appendingPathComponent("item.pdf")
        try Data("body".utf8).write(to: file)
        _ = try accessStore.adopt(
            accessRef: "access-1",
            linkedDocumentID: "document-item-1",
            selectedURL: file,
            route: .directFile
        )
        let reader = TestOriginalReader { url in
            let data = try Data(contentsOf: url)
            _ = try accessStore.disconnect(accessRef: "access-1")
            _ = try accessStore.adopt(
                accessRef: "access-1",
                linkedDocumentID: "document-item-1",
                selectedURL: file,
                route: .directFile
            )
            return data
        }
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: reader
        )
        let item = CapturePreparationItem(
            access: NativeAccessExpectation(accessRef: "access-1", generation: 1),
            seed: seed(item: "item-1", access: "access-1", generation: 1)
        )

        do {
            _ = try await coordinator.prepareAndPublish([item])
            XCTFail("Expected generation mismatch")
        } catch {
            XCTAssertEqual(error as? NativeAccessError, .generationMismatch)
        }
        XCTAssertTrue(queue.allEntries().isEmpty)
    }

    func testBindingMismatchFailsBeforeReadingOrPublishing() async throws {
        let root = try temporaryRoot()
        let accessStore = try NativeAccessStore(
            stateDirectory: root.appendingPathComponent("access"),
            codec: PreparationBookmarkCodec()
        )
        let queue = try CaptureIntentQueue(root: root.appendingPathComponent("queue"))
        let coordinator = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: TestOriginalReader { _ in XCTFail("must not read"); return Data() }
        )
        let item = CapturePreparationItem(
            access: NativeAccessExpectation(accessRef: "other-access", generation: 1),
            seed: seed(item: "item-1", access: "access-1", generation: 1)
        )

        do {
            _ = try await coordinator.prepareAndPublish([item])
            XCTFail("Expected invalid binding")
        } catch {
            XCTAssertEqual(error as? CapturePreparationError, .invalidBinding)
        }
        XCTAssertTrue(queue.allEntries().isEmpty)
    }
}
