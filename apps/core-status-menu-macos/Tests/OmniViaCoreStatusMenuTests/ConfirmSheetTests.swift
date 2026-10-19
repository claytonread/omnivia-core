// The confirming sheets: what they run, and when.
//
// The cancel hook is the one with teeth. A level change is *proposed* by a
// picker, so dismissing the sheet has to put that picker back — otherwise the
// row shows a level the service was never asked for.

import AppKit
import XCTest

@testable import CoreSettingsMacOS

@MainActor
final class ConfirmSheetTests: XCTestCase {
    private func sheet(
        onCancel: (() -> Void)? = nil, onConfirm: @escaping () -> Void = {}
    ) -> ConfirmSheet {
        ConfirmSheet(
            title: "Remove contribution permission?",
            subtitle: "Codex returns to Read only.",
            body: "It keeps retrieving what it is allowed to access.",
            confirm: "Remove permission",
            onCancel: onCancel,
            onConfirm: onConfirm
        )
    }

    func testCancellingRunsTheCancelHook() {
        var restored = 0
        sheet(onCancel: { restored += 1 }).cancel()
        XCTAssertEqual(restored, 1, "the control that proposed the change must be put back")
    }

    func testCancellingDoesNotConfirm() {
        var confirmed = false
        sheet(onCancel: {}, onConfirm: { confirmed = true }).cancel()
        XCTAssertFalse(confirmed)
    }

    /// A sheet with nothing to put back is still cancellable.
    func testCancellingWithoutAHookIsHarmless() {
        sheet().cancel()
    }

    func testTheBodyCarriesTheProseAndTheFooterTheTwoButtons() {
        let views = sheet().makeBody()
        XCTAssertEqual(views.count, 1)
        let buttons = sheet().makeFoot().compactMap { $0 as? NSButton }
        XCTAssertEqual(buttons.map(\.title), ["Cancel", "Remove permission"])
    }

    /// The checks state what a level allows and, as pointedly, what it does
    /// not; the note is the design's closing line.
    func testChecksAndNoteBecomeBodyRows() {
        let promote = ConfirmSheet(
            title: "Allow Codex to contribute?",
            subtitle: "Workspace: ws-1",
            checks: [("Keeps searching.", true, nil), ("Cannot approve.", false, "Stays with you.")],
            note: "Core records this authorisation.",
            confirm: "Allow contributions",
            onConfirm: {}
        )
        XCTAssertEqual(promote.makeBody().count, 3, "two checks and the note, with no prose")
    }

    func testLevelsMapToTheProfilesConfigureTakes() {
        XCTAssertEqual(AccessLevel.named("authoring").name, "Read and contribute")
        XCTAssertEqual(AccessLevel.named("restricted").name, "Read only")
        // Anything unrecognised reads as the lower level, never the higher.
        XCTAssertEqual(AccessLevel.named("something-new").profile, "restricted")
    }
}
