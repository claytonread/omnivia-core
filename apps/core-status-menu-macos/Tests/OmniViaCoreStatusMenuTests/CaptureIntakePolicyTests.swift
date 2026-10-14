import CoreCaptureMacOS
import DocumentIntakeMacOS
import Foundation
import XCTest

final class CaptureIntakePolicyTests: XCTestCase {
    func testOrdinaryFilePickerChoosesManyFilesAndNoDirectory() {
        let policy = CapturePickerPolicy.files

        XCTAssertTrue(policy.canChooseFiles)
        XCTAssertFalse(policy.canChooseDirectories)
        XCTAssertTrue(policy.allowsMultipleSelection)
        XCTAssertFalse(policy.treatsFilePackagesAsDirectories)
        XCTAssertFalse(policy.resolvesAliases)
    }

    func testFolderPickerChoosesOneDirectoryAndNoFile() {
        let policy = CapturePickerPolicy.folder

        XCTAssertFalse(policy.canChooseFiles)
        XCTAssertTrue(policy.canChooseDirectories)
        XCTAssertFalse(policy.allowsMultipleSelection)
        XCTAssertFalse(policy.treatsFilePackagesAsDirectories)
        XCTAssertFalse(policy.resolvesAliases)
    }

    func testNoSurfaceAdmitsMoreThanOneReviewDraftHolds() {
        XCTAssertEqual(
            CaptureIntakePolicy.maximumSelectionItems,
            CaptureDraft.maximumOrdinaryItems
        )
        XCTAssertNil(CaptureIntakePolicy.admit(itemCount: 1, isReady: true))
        XCTAssertNil(
            CaptureIntakePolicy.admit(
                itemCount: CaptureIntakePolicy.maximumSelectionItems,
                isReady: true
            )
        )
        XCTAssertEqual(
            CaptureIntakePolicy.admit(
                itemCount: CaptureIntakePolicy.maximumSelectionItems + 1,
                isReady: true
            ),
            .tooManySelected
        )
        XCTAssertEqual(CaptureIntakePolicy.admit(itemCount: 0, isReady: true), .emptySelection)
    }

    func testAnUnreadyCompanionAdmitsNothing() {
        XCTAssertEqual(CaptureIntakePolicy.admit(itemCount: 1, isReady: false), .notReady)
        XCTAssertEqual(CaptureIntakePolicy.admit(itemCount: 0, isReady: false), .notReady)
    }

    func testRefusalMessagesNameNoSuppliedWord() {
        for refusal in [
            CaptureIntakeRefusal.notReady,
            .emptySelection,
            .tooManySelected,
        ] {
            XCTAssertFalse(refusal.message.isEmpty)
            XCTAssertFalse(refusal.message.contains("/"))
        }
        XCTAssertTrue(CaptureIntakeRefusal.tooManySelected.message.contains("10"))
    }

    func testMixedSelectionIsClassifiedInTheOrderItWasGiven() throws {
        let root = try temporaryDirectory()
        let folder = root.appendingPathComponent("records", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let brief = root.appendingPathComponent("brief.pdf")
        let archive = root.appendingPathComponent("archive.bin")
        let note = root.appendingPathComponent("note.txt")
        for file in [brief, archive, note] { try Data("x".utf8).write(to: file) }
        var draft = CaptureDraft()

        try draft.add([brief, folder, archive, note])

        XCTAssertEqual(draft.items.map(\.url.lastPathComponent), [
            "brief.pdf", "records", "archive.bin", "note.txt",
        ])
        XCTAssertEqual(draft.items.map(\.kind), [
            .file(.pdf), .folder, .unsupported, .file(.text),
        ])
    }

    func testRefusedItemStaysVisibleAndBlocksSubmissionUntilRemoved() throws {
        let root = try temporaryDirectory()
        let archive = root.appendingPathComponent("archive.bin")
        let note = root.appendingPathComponent("note.txt")
        for file in [archive, note] { try Data("x".utf8).write(to: file) }
        var draft = CaptureDraft()

        try draft.add([archive, note])

        XCTAssertTrue(draft.containsUnsupported)
        XCTAssertFalse(draft.canSubmitFiles)
        XCTAssertEqual(draft.fileCandidates.map(\.displayName), ["note.txt"])

        let removed = draft.remove(at: 0)

        XCTAssertEqual(removed?.kind, .unsupported)
        XCTAssertNil(removed?.candidate())
        XCTAssertFalse(draft.containsUnsupported)
        XCTAssertTrue(draft.canSubmitFiles)
    }

    func testASelectionHoldingGrantsIsStillRefusedWholeRatherThanRetained() throws {
        let root = try temporaryDirectory()
        let archive = root.appendingPathComponent("archive.bin")
        let note = root.appendingPathComponent("note.txt")
        for file in [archive, note] { try Data("x".utf8).write(to: file) }
        var draft = CaptureDraft()

        // The caller releases the whole selection when the draft refuses it, so
        // a selection naming durable records must not leave part of itself in
        // review as a refused row.
        XCTAssertThrowsError(try draft.add([
            ManualCaptureCandidate(
                url: note,
                surface: .finderQuickAction,
                native: NativeCandidateKey(nativeRequestID: "r-1", candidateRef: "c-1")
            ),
            ManualCaptureCandidate(
                url: archive,
                surface: .finderQuickAction,
                native: NativeCandidateKey(nativeRequestID: "r-1", candidateRef: "c-2")
            ),
        ])) { error in
            XCTAssertEqual(error as? CaptureDraftError, .unsupportedItem)
        }
        XCTAssertTrue(draft.items.isEmpty)
    }

    func testAFolderIsNeverSubmittedAlongsideOrdinaryFiles() throws {
        let root = try temporaryDirectory()
        let folder = root.appendingPathComponent("records", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let note = root.appendingPathComponent("note.txt")
        try Data("x".utf8).write(to: note)
        var draft = CaptureDraft()

        try draft.add([note, folder])

        XCTAssertFalse(draft.canSubmitFiles)
        draft.remove(at: 1)
        XCTAssertTrue(draft.canSubmitFiles)
    }

    func testEveryNativeSurfaceKeepsItsOwnProvenanceThroughReview() throws {
        let root = try temporaryDirectory()
        let note = root.appendingPathComponent("note.txt")
        try Data("x".utf8).write(to: note)

        for surface in [
            CaptureSurface.finderService,
            .finderQuickAction,
            .menuDrop,
            .capturePanelDrop,
            .manual,
        ] {
            var draft = CaptureDraft()
            try draft.add([note], surface: surface)

            // The surface a selection came in on is the provenance a capture
            // carries. No surface is rewritten as another on the way to review.
            XCTAssertEqual(draft.fileCandidates.map(\.surface), [surface])
            XCTAssertEqual(draft.fileCandidates.map(\.source), [.original])
            XCTAssertFalse(draft.containsSnapshots)
            XCTAssertNil(draft.items.first?.native)
        }
    }

    func testDroppedTextAndLinksAreReviewedAsCapturedCopies() throws {
        let root = try temporaryDirectory()
        let copy = root.appendingPathComponent("Dropped Text 1.txt")
        try Data("dropped".utf8).write(to: copy)
        var draft = CaptureDraft()

        // Dropped text, a dropped link and a promised file have no original to
        // keep, so the drop reviews each as a captured copy. The panel reads
        // `containsSnapshots` to turn keep-updated off for them.
        try draft.add([copy], surface: .menuDrop, source: .capturedSnapshot)

        XCTAssertTrue(draft.containsSnapshots)
        XCTAssertTrue(draft.canSubmitFiles)
        XCTAssertEqual(draft.fileCandidates.map(\.surface), [.menuDrop])
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
