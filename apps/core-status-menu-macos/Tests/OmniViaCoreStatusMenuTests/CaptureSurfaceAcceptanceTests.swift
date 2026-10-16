import AppKit
import Foundation
import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

/// The user-visible half of five acceptance gates, each against the production
/// path the gate names rather than a restatement of it.
///
/// * AT-05 a typed note survives a later file drop, with no silent mode or
///   destination change.
/// * AT-10 a pasted filesystem path is text, and is never opened as a file.
/// * AT-11 an explicit Add while nothing can deliver is durable, and every word
///   shown for it says queued — never added, and never a destination.
/// * AT-19 an unknown or timed-out submission is shown as Checking status.
/// * AT-37 a preparation failure publishes none of the batch — no queue entry,
///   no watch, no retained grant — and keeps the reviewed draft for correction,
///   while a batch the queue has committed is never reported as a failure.
@MainActor
final class CaptureSurfaceAcceptanceTests: NativeHandoffTestCase {
    // MARK: - AT-05 · a note survives a later drop

    func testTypedNoteSurvivesALaterFileDropWithNoModeOrDestinationChange() throws {
        let root = try temporaryDirectory()
        let companion = try launch(root: root, withPanel: true)
        let panel = try XCTUnwrap(companion.panel)
        panel.reviewNote = "Compare against last quarter, see page 3."
        panel.reviewKeepUpdated = true
        let destination = panel.reviewDestinationName

        panel.addIncoming(
            [
                try writeOriginal(root, name: "alpha.txt"),
                try writeOriginal(root, name: "beta.txt"),
            ],
            surface: .capturePanelDrop
        )

        XCTAssertEqual(panel.reviewItemNames, ["alpha.txt", "beta.txt"])
        XCTAssertEqual(panel.reviewNote, "Compare against last quarter, see page 3.")
        XCTAssertTrue(panel.reviewKeepUpdated, "the drop must not silently change the mode")
        XCTAssertEqual(panel.reviewDestinationName, destination)
        // Nothing was published by the drop either: the note is still the
        // person's to edit before they confirm.
        XCTAssertEqual(companion.environment.queue.allEntries().count, 0)
    }

    /// The same gate across a closed panel. Closing review keeps the draft, and
    /// the next incoming item reopens the panel — the one path that rebuilds its
    /// chrome, which is where a reset would throw the note and the mode away.
    /// This is the only test here that builds a real window, because the chrome
    /// is what it is about.
    func testAPanelReopenedByIncomingWorkKeepsTheNoteModeAndDestination() throws {
        // Building chrome needs a window server; a session without one cannot
        // run this, and must not bring the process down trying.
        try XCTSkipIf(NSScreen.screens.isEmpty, "the panel's chrome needs a window server")
        _ = NSApplication.shared
        let root = try temporaryDirectory()
        let companion = try launch(root: root, withPanel: true)
        // A window-opening panel over the same submitter, as `main.swift` builds
        // it; the shared harness panel deliberately opens none.
        let panel = CapturePanelController(
            workspaceName: "workspace-1",
            submitter: companion.submitter
        )
        panel.openOrFocus()
        panel.reviewNote = "Compare against last quarter, see page 3."
        panel.reviewKeepUpdated = true
        let destination = panel.reviewDestinationName

        // Cancel closes the window and keeps the draft; a share then reopens it.
        panel.windowWillClose(Notification(name: NSWindow.willCloseNotification))
        panel.addIncoming([try writeOriginal(root, name: "alpha.txt")], surface: .share)

        XCTAssertEqual(panel.reviewItemNames, ["alpha.txt"])
        XCTAssertEqual(panel.reviewNote, "Compare against last quarter, see page 3.")
        XCTAssertTrue(panel.reviewKeepUpdated, "reopening must not silently change the mode")
        XCTAssertEqual(panel.reviewDestinationName, destination)
    }

    // MARK: - AT-10 · a pasted path is text

    /// Pasting a path into the note adds nothing to review, takes no grant and
    /// opens nothing. The characters stay characters.
    func testAPastedPathInTheNoteIsTextAndOpensNothing() throws {
        let root = try temporaryDirectory()
        let secret = try writeOriginal(root, name: "secret.txt", text: "SECRET-BYTES")
        let companion = try launch(root: root, withPanel: true)
        let panel = try XCTUnwrap(companion.panel)

        panel.reviewNote = "Read \(secret.path) and /etc/passwd before replying."

        XCTAssertEqual(panel.reviewNote, "Read \(secret.path) and /etc/passwd before replying.")
        XCTAssertEqual(panel.reviewItemCount, 0)
        XCTAssertEqual(companion.environment.queue.allEntries().count, 0)
        XCTAssertEqual(companion.connectedAccessRefs, [])
        XCTAssertEqual(try ownedRequests(root), [])
    }

    /// A share whose text happens to be a filesystem path is captured as that
    /// path's characters. The file it names is never opened, so its bytes reach
    /// review nowhere.
    func testASharedFilesystemPathIsAdoptedAsItsOwnTextAndNeverRead() throws {
        let root = try temporaryDirectory()
        let secret = try writeOriginal(root, name: "secret.txt", text: "SECRET-BYTES")
        try publishShare(
            try shareInbox(root),
            [("Shared Text 1.txt", secret.path)],
            kind: .text
        )

        let companion = try launch(root: root, withPanel: true)
        companion.shareAdopter.reconcile()

        let candidate = try XCTUnwrap(companion.accepted.batches.first?.first)
        let adopted = try Data(contentsOf: candidate.url)
        XCTAssertEqual(adopted, Data(secret.path.utf8))
        XCTAssertFalse(
            String(decoding: adopted, as: UTF8.self).contains("SECRET-BYTES"),
            "the named file's contents must never be read in"
        )
        XCTAssertEqual(companion.environment.queue.allEntries().count, 0)
        XCTAssertEqual(companion.environment.fileSubscriptions.all().count, 0)
    }

    // MARK: - AT-11 · queued, never added, never a destination

    /// Nothing delivers here, which is what a Core this Mac cannot reach looks
    /// like from review: the Add is durable locally and reported as queued.
    func testAnAddWithNothingToDeliverToIsDurableAndReportedAsQueuedOnly() async throws {
        let root = try temporaryDirectory()
        let companion = try launch(root: root, withPanel: true)
        let panel = try XCTUnwrap(companion.panel)
        panel.addIncoming([try writeOriginal(root, name: "invoice.txt", text: "invoice")])

        await panel.beginSubmit()?.value

        let entries = companion.environment.queue.allEntries()
        XCTAssertEqual(entries.map(\.state), [.pending])
        // Durable without anything running: the frozen bytes are on disk.
        let payload = try companion.environment.queue.payloadURL(for: entries[0].intent.itemId)
        XCTAssertEqual(try Data(contentsOf: payload), Data("invoice".utf8))
        // The destination review shows is the one this work is bound to, so
        // leaving it out of the wording hides nothing and names nothing else.
        XCTAssertEqual(entries[0].intent.workspaceRef, panel.reviewDestinationName)

        let receipt = try XCTUnwrap(panel.receipts.last)
        XCTAssertEqual(receipt.outcome, .queued(queued: 1))
        XCTAssertEqual(receipt.detail, ShareInboxStore.queuedWording)
        XCTAssertFalse(
            receipt.summary.lowercased().contains("added"),
            "a queued item is never reported as added"
        )
        XCTAssertFalse(
            receipt.summary.contains(panel.reviewDestinationName),
            "the queue wording names no destination"
        )
    }

    /// The one sentence a durable Share request is reported with, and the
    /// request it describes. Both are silent about where anything will go.
    func testADurableShareRequestIsQueuedWordingAndNamesNoDestination() throws {
        XCTAssertEqual(
            ShareInboxStore.queuedWording,
            "Queued on this Mac. Open OmniVia Core to send."
        )
        XCTAssertFalse(ShareInboxStore.queuedWording.lowercased().contains("added"))

        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let requestID = try publishShare(inbox, [("alpha.txt", "alpha")])

        // Complete and readable with no companion in the picture.
        XCTAssertEqual(inbox.pendingRequestIDs(), [requestID])
        let manifest = String(
            decoding: try Data(
                contentsOf: pendingDirectory(root, requestID: requestID)
                    .appendingPathComponent("manifest.json")
            ),
            as: UTF8.self
        ).lowercased()
        for named in ["destination", "workspace", "target", "principal"] {
            XCTAssertFalse(manifest.contains(named), "the request names no \(named)")
        }
    }

    // MARK: - AT-19 · an unknown outcome reads as Checking

    func testATimedOutSubmissionIsShownAsCheckingStatus() async throws {
        let root = try temporaryDirectory()
        let companion = try launch(root: root, withPanel: true)
        let panel = try XCTUnwrap(companion.panel)
        panel.addIncoming([try writeOriginal(root, name: "invoice.txt", text: "invoice")])
        await panel.beginSubmit()?.value

        // The attempt was made and its outcome never came back.
        let delivery = CaptureDeliveryCoordinator(
            queue: companion.environment.queue,
            transport: TimingOutTransport()
        )
        let outcomes = await delivery.deliverPending()
        XCTAssertEqual(outcomes.map(\.state), [.unknownOutcome])

        let records = try companion.environment.captureActivity.observe(
            queue: companion.environment.queue.allEntries(),
            runs: []
        )
        let group = try XCTUnwrap(
            FilesAndFoldersModel.activityGroups(records: records, runs: []).first
        )
        let item = FilesAndFoldersItem.activity(group)

        XCTAssertEqual(item.statusLabel, "Checking status…")
        XCTAssertEqual(item.activityText, "1 checking status")
        XCTAssertEqual(item.statusFilter, .needsAttention)
        // Nothing was removed and no replacement key was allocated.
        XCTAssertEqual(records.count, 1)
        XCTAssertFalse(records[0].isReceipt)
        XCTAssertEqual(records[0].itemID, outcomes[0].intent.itemId)
    }

    // MARK: - AT-37 · a failed preparation keeps the draft

    func testAPreparationFailurePublishesNoneOfTheBatchAndKeepsTheDraft() async throws {
        let root = try temporaryDirectory()
        let companion = try launch(root: root, withPanel: true)
        let panel = try XCTUnwrap(companion.panel)
        let alpha = try writeOriginal(root, name: "alpha.txt", text: "alpha")
        let beta = try writeOriginal(root, name: "beta.txt", text: "beta")
        panel.addIncoming([alpha, beta])
        panel.reviewNote = "Both of these belong to the April run."
        // Keep updated, so the batch would also establish watches: a failure
        // must leave none of those either.
        panel.reviewKeepUpdated = true
        // One item cannot be prepared, so the submission fails before it
        // publishes anything.
        try FileManager.default.removeItem(at: beta)

        await panel.beginSubmit()?.value

        XCTAssertEqual(companion.environment.queue.allEntries(), [])
        // Nothing claims this Mac is keeping a never-added file updated, and no
        // grant adopted for the attempt is still held.
        XCTAssertEqual(companion.environment.fileSubscriptions.all(), [])
        XCTAssertEqual(companion.connectedAccessRefs, [])
        // The reviewed draft is still there to correct, note and mode and all.
        XCTAssertEqual(panel.reviewItemNames, ["alpha.txt", "beta.txt"])
        XCTAssertEqual(panel.reviewNote, "Both of these belong to the April run.")
        XCTAssertTrue(panel.reviewKeepUpdated)
        let receipt = try XCTUnwrap(panel.receipts.last)
        XCTAssertEqual(receipt.outcome, .failed)
    }

    /// The other side of the same boundary: once the queue has committed, the
    /// add happened. A watch that could not be recorded after that commit is
    /// reported as queued with what went unrecorded — never as a failure, which
    /// would invite a retry that adds the same file twice.
    func testACommittedBatchWhoseWatchCannotBeRecordedIsStillReportedAsQueued() async throws {
        let root = try temporaryDirectory()
        let companion = try launch(root: root, withPanel: true)
        let panel = try XCTUnwrap(companion.panel)
        panel.addIncoming([try writeOriginal(root, name: "invoice.txt", text: "invoice")])
        panel.reviewKeepUpdated = true

        let subscriptions = root.appendingPathComponent("native-intake", isDirectory: true)
            .appendingPathComponent("file-subscriptions", isDirectory: true)
        try await withPermissions(0o500, on: subscriptions) {
            await panel.beginSubmit()?.value
        }

        XCTAssertEqual(companion.environment.queue.allEntries().map(\.state), [.pending])
        XCTAssertEqual(companion.environment.fileSubscriptions.all(), [])
        let receipt = try XCTUnwrap(panel.receipts.last)
        XCTAssertEqual(receipt.outcome, .queued(queued: 1))
        XCTAssertTrue(receipt.detail.contains("Queued on this Mac."))
        XCTAssertTrue(
            receipt.detail.contains("Keeping them updated couldn’t be recorded, "
                + "so these are queued once only.")
        )
        // Still queued, never added, and no destination named.
        XCTAssertFalse(receipt.summary.lowercased().contains("added"))
        XCTAssertFalse(receipt.summary.contains(panel.reviewDestinationName))
        // Published work leaves review, so the person cannot add it again by
        // confirming the same draft.
        XCTAssertEqual(panel.reviewItemNames, [])
    }
}

/// A transport whose attempt never comes back with an answer.
///
/// `.unknown` is exactly what the production transport returns when its own
/// deadline passes with the CLI still running, or when the answer cannot be
/// read — see `CLICaptureDelivery.deliver`. Driving that path for real would
/// mean waiting out a 25-second deadline, so the outcome stands in for it.
private struct TimingOutTransport: CaptureDeliveryTransport {
    let descriptorDigest = "sha256:" + String(repeating: "b", count: 64)
    let deliveryBinding = CaptureDeliveryBinding.companion(workspaceRef: "workspace-1")

    func deliver(input: Data, payloadURL: URL, operationKey: String) async -> CaptureDeliveryOutcome {
        .unknown
    }
}
