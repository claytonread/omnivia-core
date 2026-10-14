import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

/// END-04: ordinary files, an import-once folder and a keep-updated folder in
/// one selection are three groups. Each is confirmed on its own, publishes on
/// its own and reports its own receipt; none claims the whole selection.
@MainActor
final class MixedGroupIntakeTests: NativeHandoffTestCase {
    private enum Group: CaseIterable { case files, once, watch }

    private struct Selection {
        let companion: Companion
        let once: URL
        let watch: URL
    }

    private static let importOnce = FolderCapturePolicy(mode: .importOnce, recursive: true, includeHidden: false)
    private static let keepUpdated = FolderCapturePolicy(mode: .keepUpdated, recursive: true, includeHidden: false)

    /// Order given: brief.pdf, Once, notes.txt, Watch.
    private func deliverMixedSelection(_ root: URL) async throws -> Selection {
        let first = try writeOriginal(root, name: "brief.pdf")
        let once = try makeFolder(root, name: "Once", files: ["once-member.pdf"])
        let last = try writeOriginal(root, name: "notes.txt")
        let watch = try makeFolder(root, name: "Watch", files: ["watch-member.pdf"])
        let companion = try launch(root: root, withPanel: true, withFolders: true)
        _ = try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(
                try finderOffer(
                    nonce: newID(),
                    slotIDs: [newID(), newID(), newID(), newID()],
                    urls: [first, once, last, watch]
                )
            )
        )
        let panel = try XCTUnwrap(companion.panel)
        XCTAssertEqual(panel.reviewItemNames, ["brief.pdf", "Once", "notes.txt", "Watch"])
        return Selection(companion: companion, once: once, watch: watch)
    }

    private func confirm(_ group: Group, _ selection: Selection) async throws -> CaptureGroupReceipt {
        let panel = try XCTUnwrap(selection.companion.panel)
        let folders = try XCTUnwrap(selection.companion.folders)
        switch group {
        case .files:
            let task = try XCTUnwrap(panel.beginSubmit())
            await task.value
        case .once:
            _ = await panel.confirmFolder(
                try folders.plan(root: selection.once, policy: Self.importOnce),
                removingReviewRow: selection.once
            )
        case .watch:
            _ = await panel.confirmFolder(
                try folders.plan(root: selection.watch, policy: Self.keepUpdated),
                removingReviewRow: selection.watch
            )
        }
        return try XCTUnwrap(panel.receipts.last)
    }

    private func submissions(_ companion: Companion) -> [String: [String]] {
        Dictionary(grouping: companion.environment.queue.allEntries(), by: \.intent.submissionId)
            .mapValues { $0.compactMap(\.intent.displayName).sorted() }
    }

    func testEachGroupIsConfirmedAndReceiptedIndependentlyInAnyOrder() async throws {
        let orders: [[Group]] = [[.files, .once, .watch], [.watch, .files, .once], [.once, .watch, .files]]
        for order in orders {
            let selection = try await deliverMixedSelection(try temporaryDirectory())
            let panel = try XCTUnwrap(selection.companion.panel)
            var confirmed: [CaptureGroupReceipt] = []

            for (position, group) in order.enumerated() {
                let receipt = try await confirm(group, selection)
                confirmed.append(receipt)

                // Only the group just confirmed has published anything new, and
                // every earlier group's receipt is exactly as it was.
                XCTAssertEqual(submissions(selection.companion).count, position + 1, "\(order)")
                XCTAssertEqual(panel.receipts, confirmed, "\(order)")
                XCTAssertEqual(panel.reviewItemCount, 4 - confirmedRows(order[...position]), "\(order)")
            }

            XCTAssertEqual(
                Set(confirmed.map(\.kind)),
                [.ordinaryFiles, .folderImportOnce, .folderKeepUpdated]
            )
            XCTAssertEqual(Set(confirmed.map(\.id)).count, 3)
            let ids = confirmed.compactMap(\.submissionID)
            XCTAssertEqual(Set(ids).count, 3, "each group has its own submission identity")
            let queued = submissions(selection.companion)
            for receipt in confirmed {
                let id = try XCTUnwrap(receipt.submissionID)
                switch receipt.kind {
                case .ordinaryFiles: XCTAssertEqual(queued[id], ["brief.pdf", "notes.txt"])
                case .folderImportOnce: XCTAssertEqual(queued[id], ["once-member.pdf"])
                case .folderKeepUpdated: XCTAssertEqual(queued[id], ["watch-member.pdf"])
                }
                XCTAssertEqual(receipt.outcome, .queued(queued: queued[id]?.count ?? -1))
            }
            XCTAssertEqual(panel.reviewItemCount, 0)
        }
    }

    private func confirmedRows(_ groups: ArraySlice<Group>) -> Int {
        groups.reduce(0) { $0 + ($1 == .files ? 2 : 1) }
    }

    func testConfirmingAFolderNeverSubmitsThePendingFilesOrTheOtherFolder() async throws {
        let selection = try await deliverMixedSelection(try temporaryDirectory())
        let panel = try XCTUnwrap(selection.companion.panel)

        let receipt = try await confirm(.watch, selection)

        XCTAssertEqual(receipt.kind, .folderKeepUpdated)
        XCTAssertEqual(submissions(selection.companion).values.flatMap { $0 }, ["watch-member.pdf"])
        XCTAssertEqual(panel.reviewItemNames, ["brief.pdf", "Once", "notes.txt"])
        // The other folder's root grant is still only prepared, never enrolled.
        XCTAssertEqual(selection.companion.connectedRoots.count, 2)
    }

    func testConfirmingTheFilesNeverSubmitsAPendingFolder() async throws {
        let selection = try await deliverMixedSelection(try temporaryDirectory())
        let panel = try XCTUnwrap(selection.companion.panel)

        let receipt = try await confirm(.files, selection)

        XCTAssertEqual(receipt.kind, .ordinaryFiles)
        XCTAssertEqual(receipt.title, "2 files")
        XCTAssertEqual(submissions(selection.companion).values.flatMap { $0 }.sorted(), ["brief.pdf", "notes.txt"])
        // Both folders stay in review, in order, with no other claim made.
        XCTAssertEqual(panel.reviewItemNames, ["Once", "Watch"])
        XCTAssertEqual(panel.receipts.count, 1)
    }

    func testCancellingOneFolderIsReportedForThatFolderAlone() async throws {
        let selection = try await deliverMixedSelection(try temporaryDirectory())
        let panel = try XCTUnwrap(selection.companion.panel)

        panel.declineFolder(selection.once, mode: .importOnce)
        let files = try await confirm(.files, selection)
        let watch = try await confirm(.watch, selection)

        XCTAssertEqual(panel.receipts.map(\.kind), [.folderImportOnce, .ordinaryFiles, .folderKeepUpdated])
        XCTAssertEqual(panel.receipts[0].outcome, .cancelled)
        XCTAssertNil(panel.receipts[0].submissionID)
        XCTAssertEqual(files.outcome, .queued(queued: 2))
        XCTAssertEqual(watch.outcome, .queued(queued: 1))
        // The declined folder is still in review and published nothing.
        XCTAssertEqual(panel.reviewItemNames, ["Once"])
        XCTAssertFalse(submissions(selection.companion).values.flatMap { $0 }.contains("once-member.pdf"))
    }

    func testOneFailedGroupDoesNotFailOrCancelTheOthers() async throws {
        let selection = try await deliverMixedSelection(try temporaryDirectory())
        let panel = try XCTUnwrap(selection.companion.panel)
        let folders = try XCTUnwrap(selection.companion.folders)
        // A scope over its reviewed bound enrols nothing.
        let tooLarge = FolderCaptureSubmission(
            environment: selection.companion.environment,
            workspaceRef: "workspace-1",
            limits: FolderScopeLimits(maximumEligibleFiles: 0, maximumExaminedEntries: 100, maximumDepth: 4)
        )
        let overLimit = try tooLarge.plan(root: selection.once, policy: Self.importOnce)
        XCTAssertNotNil(overLimit.limit)

        let failed = await panel.confirmFolder(overLimit, removingReviewRow: selection.once)
        let files = try await confirm(.files, selection)
        let watch = try await confirm(.watch, selection)

        XCTAssertEqual(failed.outcome, .failed)
        XCTAssertNil(failed.submissionID)
        XCTAssertEqual(files.outcome, .queued(queued: 2))
        XCTAssertEqual(watch.outcome, .queued(queued: 1))
        XCTAssertEqual(panel.receipts, [failed, files, watch])
        // The failed folder keeps its row so the person can retry it alone.
        XCTAssertEqual(panel.reviewItemNames, ["Once"])
        let retried = await panel.confirmFolder(
            try folders.plan(root: selection.once, policy: Self.importOnce),
            removingReviewRow: selection.once
        )
        XCTAssertEqual(retried.outcome, .queued(queued: 1))
        XCTAssertEqual(panel.reviewItemNames, [])
    }

    func testAFailedFileGroupLeavesTheFoldersPendingAndConfirmable() async throws {
        let root = try temporaryDirectory()
        let selection = try await deliverMixedSelection(root)
        let panel = try XCTUnwrap(selection.companion.panel)
        try FileManager.default.removeItem(at: root.appendingPathComponent("notes.txt"))

        let files = try await confirm(.files, selection)
        let watch = try await confirm(.watch, selection)

        XCTAssertEqual(files.kind, .ordinaryFiles)
        XCTAssertEqual(files.outcome, .failed)
        XCTAssertNil(files.submissionID)
        XCTAssertEqual(watch.outcome, .queued(queued: 1))
        XCTAssertEqual(panel.receipts, [files, watch])
        XCTAssertEqual(panel.reviewItemNames, ["brief.pdf", "Once", "notes.txt"])
    }

    func testAGroupCannotBeConfirmedTwiceWhileItsConfirmationIsInFlight() async throws {
        let selection = try await deliverMixedSelection(try temporaryDirectory())
        let panel = try XCTUnwrap(selection.companion.panel)

        let task = try XCTUnwrap(panel.beginSubmit())
        XCTAssertNil(panel.beginSubmit())
        await task.value

        XCTAssertEqual(panel.receipts.count, 1)
        XCTAssertEqual(submissions(selection.companion).count, 1)
    }

    func testARestartKeepsEachConfirmedGroupDurableAndRestoresOnlyTheUnconfirmedOnes() async throws {
        let root = try temporaryDirectory()
        let selection = try await deliverMixedSelection(root)
        let files = try await confirm(.files, selection)
        let watch = try await confirm(.watch, selection)
        let before = submissions(selection.companion)

        let restarted = try launch(root: root, withPanel: true, withFolders: true)
        restarted.receiver.reconcile()
        let panel = try XCTUnwrap(restarted.panel)

        // The confirmed groups are durable queue work; they are not review rows
        // again and the unconfirmed folder is the only thing review restores.
        XCTAssertEqual(submissions(restarted), before)
        XCTAssertEqual(Set(before.keys), Set([files.submissionID, watch.submissionID].compactMap { $0 }))
        XCTAssertEqual(panel.reviewItemNames, ["Once"])
        XCTAssertEqual(panel.receipts, [])
        let once = await panel.confirmFolder(
            try XCTUnwrap(restarted.folders).plan(root: selection.once, policy: Self.importOnce),
            removingReviewRow: selection.once
        )
        XCTAssertEqual(once.outcome, .queued(queued: 1))
        XCTAssertEqual(submissions(restarted).count, 3)
        XCTAssertEqual(panel.receipts, [once])
    }
}
