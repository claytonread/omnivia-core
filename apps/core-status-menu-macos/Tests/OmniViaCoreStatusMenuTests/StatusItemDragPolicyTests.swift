import CoreCaptureMacOS
import DocumentIntakeMacOS
import Foundation
import XCTest
@testable import OmniViaCoreStatusMenu

final class StatusItemDragPolicyTests: XCTestCase {
    func testFileURLsAreBoundedByWhatOneReviewDraftHolds() {
        XCTAssertEqual(
            StatusItemDragPolicy.maximumFileURLs,
            CaptureIntakePolicy.maximumSelectionItems
        )
        XCTAssertTrue(StatusItemDragPolicy.admits(fileURLs: 1, promisedFiles: 0, texts: []))
        XCTAssertTrue(
            StatusItemDragPolicy.admits(
                fileURLs: StatusItemDragPolicy.maximumFileURLs,
                promisedFiles: 0,
                texts: []
            )
        )
        XCTAssertFalse(
            StatusItemDragPolicy.admits(
                fileURLs: StatusItemDragPolicy.maximumFileURLs + 1,
                promisedFiles: 0,
                texts: []
            )
        )
    }

    func testPromisedFilesAreBoundedByTheOwnedCopyStore() {
        XCTAssertEqual(
            StatusItemDragPolicy.maximumPromisedFiles,
            OwnedSnapshotStore.maximumItems
        )
        XCTAssertTrue(StatusItemDragPolicy.admits(fileURLs: 0, promisedFiles: 1, texts: []))
        XCTAssertFalse(
            StatusItemDragPolicy.admits(
                fileURLs: 0,
                promisedFiles: StatusItemDragPolicy.maximumPromisedFiles + 1,
                texts: []
            )
        )
    }

    func testDroppedTextIsBoundedAndNeverEmpty() {
        XCTAssertTrue(StatusItemDragPolicy.admits(fileURLs: 0, promisedFiles: 0, texts: ["note"]))
        XCTAssertFalse(StatusItemDragPolicy.admits(fileURLs: 0, promisedFiles: 0, texts: [""]))
        XCTAssertFalse(StatusItemDragPolicy.admits(fileURLs: 0, promisedFiles: 0, texts: []))
        XCTAssertFalse(
            StatusItemDragPolicy.admits(fileURLs: 0, promisedFiles: 0, texts: ["a", "b"])
        )
        let oversized = String(
            repeating: "x",
            count: StatusItemDragPolicy.maximumTextBytes + 1
        )
        XCTAssertFalse(
            StatusItemDragPolicy.admits(fileURLs: 0, promisedFiles: 0, texts: [oversized])
        )
    }

    func testAnOversizedFileSelectionIsRefusedEvenWhenTextIsAlsoOffered() {
        // A Finder drag offers the same selection as file URLs and as a string.
        // The drag is the files it names, so an over-bound selection is refused
        // rather than reviewed as its own filenames.
        XCTAssertFalse(
            StatusItemDragPolicy.admits(
                fileURLs: StatusItemDragPolicy.maximumFileURLs + 1,
                promisedFiles: 0,
                texts: ["brief.pdf"]
            )
        )
        XCTAssertFalse(
            StatusItemDragPolicy.admits(
                fileURLs: 0,
                promisedFiles: StatusItemDragPolicy.maximumPromisedFiles + 1,
                texts: ["brief.pdf"]
            )
        )
    }

    func testTheHoverAffordanceWaitsFourHundredMilliseconds() {
        XCTAssertEqual(StatusItemDragPolicy.hoverAffordanceDelay, 0.4)
    }

    func testTheAffordanceNamesTheDeclaredCountAndNoFile() {
        XCTAssertEqual(
            StatusItemDragPolicy.affordanceLabel(declaredItems: 1),
            "Drop to add to Core — 1 item"
        )
        XCTAssertEqual(
            StatusItemDragPolicy.affordanceLabel(declaredItems: 4),
            "Drop to add to Core — 4 items"
        )
        // A pasteboard that declares no count is still a drag this target
        // accepted, so it reads as the one item it is.
        XCTAssertEqual(
            StatusItemDragPolicy.affordanceLabel(declaredItems: 0),
            "Drop to add to Core — 1 item"
        )
        XCTAssertFalse(StatusItemDragPolicy.affordanceLabel(declaredItems: 3).contains("/"))
    }

    func testTheAffordanceSitsUnderTheStatusItemAndInsideTheVisibleScreen() {
        let visible = CGRect(x: 0, y: 0, width: 1_000, height: 900)
        let size = CGSize(width: 200, height: 30)

        let centred = StatusItemDragPolicy.affordanceFrame(
            size: size,
            anchor: CGRect(x: 400, y: 880, width: 24, height: 22),
            visible: visible
        )
        XCTAssertEqual(centred.midX, 412)
        XCTAssertEqual(centred.maxY, 874)

        // A status item near the right edge pulls the affordance back onto the
        // screen rather than off it.
        let clamped = StatusItemDragPolicy.affordanceFrame(
            size: size,
            anchor: CGRect(x: 980, y: 880, width: 24, height: 22),
            visible: visible
        )
        XCTAssertEqual(clamped.maxX, visible.maxX)
        XCTAssertTrue(visible.contains(clamped))

        let tight = StatusItemDragPolicy.affordanceFrame(
            size: CGSize(width: 2_000, height: 30),
            anchor: CGRect(x: 10, y: 880, width: 24, height: 22),
            visible: visible
        )
        XCTAssertEqual(tight.minX, visible.minX)
    }

    func testPromisedFilesAreReportedOnceInPromisedSourceOrder() {
        var collector = PromisedFileCollector(expectedPerReceiver: [2, 1])
        XCTAssertEqual(collector.total, 3)
        let first = URL(fileURLWithPath: "/tmp/a/1")
        let second = URL(fileURLWithPath: "/tmp/a/2")
        let third = URL(fileURLWithPath: "/tmp/a/3")

        // The second receiver finishes before the first does. The flattened
        // report is still receiver order, not callback order.
        XCTAssertNil(collector.receive(receiver: 1, url: third))
        XCTAssertNil(collector.receive(receiver: 0, url: first))

        XCTAssertEqual(
            collector.receive(receiver: 0, url: second),
            [first, second, third]
        )
        // Reported once: a redelivered callback adds no second selection.
        XCTAssertNil(collector.receive(receiver: 0, url: second))
        XCTAssertFalse(collector.abandon())
    }

    func testAFailedPromiseStillSettlesAndIsNotReported() {
        var collector = PromisedFileCollector(expectedPerReceiver: [2])
        let delivered = URL(fileURLWithPath: "/tmp/a/1")

        XCTAssertNil(collector.receive(receiver: 0, url: nil))
        XCTAssertEqual(collector.receive(receiver: 0, url: delivered), [])
        XCTAssertNil(collector.receive(receiver: 0, url: delivered))
    }

    func testAProviderThatOverrunsOrMisnamesItselfSettlesNothing() {
        var collector = PromisedFileCollector(expectedPerReceiver: [1, 1])
        let first = URL(fileURLWithPath: "/tmp/a/1")

        XCTAssertNil(collector.receive(receiver: 0, url: first))
        // A receiver calling back past its own promise must not settle the
        // other receiver's outstanding promise.
        XCTAssertNil(collector.receive(receiver: 0, url: first))
        XCTAssertNil(collector.receive(receiver: 7, url: first))

        let second = URL(fileURLWithPath: "/tmp/a/2")
        XCTAssertEqual(collector.receive(receiver: 1, url: second), [first, second])
    }

    func testATimedOutDeliveryEndsOnceAndIgnoresLateCallbacks() {
        var collector = PromisedFileCollector(expectedPerReceiver: [2])
        XCTAssertNil(collector.receive(receiver: 0, url: URL(fileURLWithPath: "/tmp/a/1")))

        // The timeout ends the drag whole: a partly delivered promise is not
        // the selection the person dragged.
        XCTAssertTrue(collector.abandon())
        XCTAssertFalse(collector.abandon())
        XCTAssertNil(collector.receive(receiver: 0, url: URL(fileURLWithPath: "/tmp/a/2")))
    }

    func testPromisedDeliveryIsBounded() {
        XCTAssertEqual(StatusItemDragPolicy.promisedDeliveryTimeout, 30)
    }

    func testOnlyContainedRegularFilesWithinTheByteQuotasAreAccepted() throws {
        let destination = try temporaryDirectory()
        let delivered = destination.appendingPathComponent("brief.pdf")
        try Data("x".utf8).write(to: delivered)

        XCTAssertEqual(
            StatusItemDragPolicy.acceptedPromisedFiles([delivered], destination: destination),
            [delivered.standardizedFileURL]
        )
        XCTAssertNil(StatusItemDragPolicy.acceptedPromisedFiles([], destination: destination))

        // A delivery outside the directory this process made, however it is
        // spelled, is refused rather than copied.
        let outside = try temporaryDirectory().appendingPathComponent("brief.pdf")
        try Data("x".utf8).write(to: outside)
        XCTAssertNil(StatusItemDragPolicy.acceptedPromisedFiles([outside], destination: destination))
        XCTAssertNil(
            StatusItemDragPolicy.acceptedPromisedFiles(
                [destination.appendingPathComponent("../escape.pdf")],
                destination: destination
            )
        )
        let nested = destination.appendingPathComponent("inner", isDirectory: true)
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        let deep = nested.appendingPathComponent("brief.pdf")
        try Data("x".utf8).write(to: deep)
        XCTAssertNil(StatusItemDragPolicy.acceptedPromisedFiles([deep], destination: destination))

        // A link is a link, never the bytes it points at, and a directory is
        // not a promised file.
        let link = destination.appendingPathComponent("link.pdf")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: delivered)
        XCTAssertNil(StatusItemDragPolicy.acceptedPromisedFiles([link], destination: destination))
        XCTAssertNil(StatusItemDragPolicy.acceptedPromisedFiles([nested], destination: destination))
        XCTAssertNil(
            StatusItemDragPolicy.acceptedPromisedFiles(
                [destination.appendingPathComponent("absent.pdf")],
                destination: destination
            )
        )
    }

    func testPerItemAndWholeSelectionBytesAreRefusedBeforeStaging() throws {
        let destination = try temporaryDirectory()
        let oversized = destination.appendingPathComponent("huge.bin")
        try sparseFile(at: oversized, bytes: OwnedSnapshotStore.maximumItemBytes + 1)

        XCTAssertNil(StatusItemDragPolicy.acceptedPromisedFiles([oversized], destination: destination))

        // Five items each at the per-item bound exceed the whole-selection
        // bound, so the drag is refused rather than part-staged.
        var withinItemBound: [URL] = []
        for index in 0..<5 {
            let url = destination.appendingPathComponent("part-\(index).bin")
            try sparseFile(at: url, bytes: OwnedSnapshotStore.maximumItemBytes)
            withinItemBound.append(url)
        }
        XCTAssertEqual(
            StatusItemDragPolicy.acceptedPromisedFiles(
                Array(withinItemBound.prefix(4)),
                destination: destination
            )?.count,
            4
        )
        XCTAssertNil(
            StatusItemDragPolicy.acceptedPromisedFiles(withinItemBound, destination: destination)
        )
    }

    private func sparseFile(at url: URL, bytes: UInt64) throws {
        FileManager.default.createFile(atPath: url.path, contents: nil)
        let handle = try FileHandle(forWritingTo: url)
        defer { try? handle.close() }
        try handle.truncate(atOffset: bytes)
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url.standardizedFileURL
    }

    func testOneDragSessionReachesReviewOnlyOnce() {
        var gate = DragSessionGate()

        XCTAssertTrue(gate.admit(session: 7))
        XCTAssertFalse(gate.admit(session: 7))
        XCTAssertFalse(gate.admit(session: 7))
        XCTAssertTrue(gate.admit(session: 8))
        XCTAssertFalse(gate.admit(session: 8))
    }
}
