// What the Add-application sheet hands to `mcp configure`.
//
// The form is built without a parent window — `makeBody()` is the whole of
// it — and `submit()` reads the same controls it would on screen.

import AppKit
import XCTest

@testable import CoreSettingsMacOS

@MainActor
final class AddApplicationSheetTests: XCTestCase {
    private func sheet(
        _ record: @escaping (String, String) -> Void
    ) -> AddApplicationSheet {
        let sheet = AddApplicationSheet(
            choices: [
                .init(host: "claude-code", displayName: "Claude Code"),
                .init(host: "codex", displayName: "Codex"),
            ],
            workspace: "ws-1",
            onSubmit: record
        )
        _ = sheet.makeBody()
        return sheet
    }

    /// The design starts every application at the lower level.
    func testDefaultsToReadOnlyAndTheFirstApplication() {
        var got: (String, String)?
        let sheet = sheet { got = ($0, $1) }
        sheet.submit()
        XCTAssertEqual(got?.0, "claude-code")
        XCTAssertEqual(got?.1, "restricted")
    }

    func testTheChosenLevelIsTheOneSubmitted() {
        var got: (String, String)?
        let sheet = sheet { got = ($0, $1) }
        sheet.pickLevel(sheet.radios[1])
        sheet.submit()
        XCTAssertEqual(got?.1, "authoring", "'Read and contribute' is the authoring profile")
    }

    /// Two radios, one answer: picking the second must clear the first, or the
    /// sheet would show a level it is not about to send.
    func testTheRadiosAreExclusive() {
        let sheet = sheet { _, _ in }
        XCTAssertEqual(sheet.radios.map(\.isOn), [true, false])
        sheet.pickLevel(sheet.radios[1])
        XCTAssertEqual(sheet.radios.map(\.isOn), [false, true])
        sheet.pickLevel(sheet.radios[0])
        XCTAssertEqual(sheet.radios.map(\.isOn), [true, false])
    }

    func testTheChosenApplicationIsTheOneSubmitted() {
        var got: (String, String)?
        let sheet = sheet { got = ($0, $1) }
        sheet.chooseApplication(at: 1)
        sheet.submit()
        XCTAssertEqual(got?.0, "codex")
    }
}
