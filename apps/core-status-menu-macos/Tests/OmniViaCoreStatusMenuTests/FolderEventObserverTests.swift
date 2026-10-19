import DocumentIntakeMacOS
import Foundation
import XCTest

final class FolderEventObserverTests: XCTestCase {
    private final class ReceivedSubscriptions: @unchecked Sendable {
        private let lock = NSLock()
        private var values = Set<String>()

        func add(_ subscriptions: Set<String>) { lock.withLock { values.formUnion(subscriptions) } }
        func snapshot() -> Set<String> { lock.withLock { values } }
    }

    func testRecursiveFileEventMapsOnlyToItsRegisteredSubscription() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-events-\(UUID().uuidString)", isDirectory: true)
        let nested = root.appendingPathComponent("nested", isDirectory: true)
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let received = expectation(description: "FSEvents signal")
        let subscriptions = ReceivedSubscriptions()
        let observer = try XCTUnwrap(
            FolderEventObserver(
                roots: [FolderObservationRoot(subscriptionID: "subscription-a", url: root)]
            ) { signal in
                subscriptions.add(signal.subscriptionIDs)
                received.fulfill()
            }
        )
        withExtendedLifetime(observer) {
            DispatchQueue.global().asyncAfter(deadline: .now() + 0.3) {
                try? Data("changed".utf8).write(to: nested.appendingPathComponent("note.txt"))
            }
            wait(for: [received], timeout: 5)
        }
        XCTAssertEqual(subscriptions.snapshot(), ["subscription-a"])
    }
}
