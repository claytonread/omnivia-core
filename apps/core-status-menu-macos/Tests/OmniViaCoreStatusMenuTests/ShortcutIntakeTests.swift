import AppIntents
import Foundation
import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

/// The two review-first App Intents. Nothing here adds, watches or queues
/// anything, and the Shortcut returns only once this companion owns copies of
/// every supplied representation.
@MainActor
final class ShortcutIntakeTests: NativeHandoffTestCase {
    private var opens = 0
    private var reviewed: [[URL]] = []

    /// Wires `ShortcutIntakeRuntime` the way `main.swift` wires it, over a
    /// real owned-copy store and a real review draft.
    private func configureRuntime(root: URL) throws -> Companion {
        let companion = try launch(root: root, withPanel: true)
        opens = 0
        reviewed = []
        ShortcutIntakeRuntime.configure(
            snapshotStore: companion.snapshots,
            openHandler: { [weak self] in self?.opens += 1 },
            reviewHandler: { [weak self] urls in
                self?.reviewed.append(urls)
                companion.panel?.addIncomingSnapshots(urls, surface: .shortcut)
            }
        )
        return companion
    }

    func testExactlyTwoShortcutsAreOffered() {
        XCTAssertEqual(OmniViaCoreShortcuts.appShortcuts.count, 2)
    }

    /// Open Add to Core opens the panel and does nothing else: no file is
    /// read, nothing is reviewed and nothing is queued.
    func testOpenAddToCoreOnlyOpensThePanel() async throws {
        let root = try temporaryDirectory()
        let companion = try configureRuntime(root: root)

        _ = try await OpenAddToCoreIntent().perform()

        XCTAssertEqual(opens, 1)
        XCTAssertEqual(reviewed, [])
        XCTAssertEqual(companion.panel?.reviewItemCount, 0)
        XCTAssertEqual(companion.environment.queue.allEntries().count, 0)
    }

    /// Every supplied representation is copied into storage this companion
    /// owns before `perform` returns, in the order it was supplied. Both a
    /// data representation and a file-URL representation end as snapshots.
    func testReviewFilesOwnsEveryRepresentationBeforeItReturns() async throws {
        let root = try temporaryDirectory()
        let companion = try configureRuntime(root: root)
        let original = try writeOriginal(root, name: "gamma.txt", text: "gamma")

        let intent = ReviewFilesInOmniViaCoreIntent()
        intent.files = [
            IntentFile(data: Data("alpha".utf8), filename: "alpha.txt"),
            IntentFile(fileURL: original, filename: "gamma.txt"),
            IntentFile(data: Data("beta".utf8), filename: "beta.txt"),
        ]
        _ = try await intent.perform()

        let urls = try XCTUnwrap(reviewed.first)
        XCTAssertEqual(urls.count, 3)
        XCTAssertEqual(
            try urls.map { try Data(contentsOf: $0) },
            [Data("alpha".utf8), Data("gamma".utf8), Data("beta".utf8)]
        )
        // The copies stand on their own: the supplied original can go away.
        try FileManager.default.removeItem(at: original)
        XCTAssertEqual(try Data(contentsOf: urls[1]), Data("gamma".utf8))
        // Review holds captured copies, and they can never be kept updated.
        XCTAssertEqual(companion.panel?.reviewItemCount, 3)
        XCTAssertEqual(companion.panel?.reviewAllowsKeepUpdated, false)
        XCTAssertEqual(companion.environment.queue.allEntries().count, 0)
        XCTAssertEqual(companion.environment.fileSubscriptions.all().count, 0)
    }

    func testReviewFilesRefusesAnEmptyOrOversizeSelection() async throws {
        let root = try temporaryDirectory()
        _ = try configureRuntime(root: root)

        let empty = ReviewFilesInOmniViaCoreIntent()
        empty.files = []
        await failure { try await empty.perform() }

        let tooMany = ReviewFilesInOmniViaCoreIntent()
        tooMany.files = (0 ... OwnedSnapshotStore.maximumItems).map { index in
            IntentFile(data: Data("x".utf8), filename: "item-\(index).txt")
        }
        await failure { try await tooMany.perform() }
        XCTAssertEqual(reviewed, [])
        XCTAssertEqual(try ownedRequests(root), [])
    }

    /// A second Shortcut run is a second selection: it waits behind the one
    /// being reviewed rather than merging into it.
    func testRepeatedShortcutRunsWaitRatherThanMerge() async throws {
        let root = try temporaryDirectory()
        let companion = try configureRuntime(root: root)

        for name in ["alpha.txt", "beta.txt"] {
            let intent = ReviewFilesInOmniViaCoreIntent()
            intent.files = [IntentFile(data: Data(name.utf8), filename: name)]
            _ = try await intent.perform()
        }

        XCTAssertEqual(reviewed.count, 2)
        XCTAssertEqual(companion.panel?.reviewItemCount, 1)
        XCTAssertEqual(companion.panel?.waitingSelectionCount, 1)
        XCTAssertEqual(companion.environment.queue.allEntries().count, 0)
    }
}
