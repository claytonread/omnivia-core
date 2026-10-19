// `pane[/detail[/ref]]`, and what a stored v1 value becomes.
//
// The migration matters because the window reopens where it was left, and a
// value written by the previous build names panes that no longer exist.

import XCTest

@testable import CoreSettingsMacOS

typealias Route = SettingsWindowController.Route

@MainActor
final class RouteTests: XCTestCase {
    func testAnEmptyOrAbsentRouteIsGeneral() {
        XCTAssertEqual(Route.parse(nil), Route(pane: .general))
        XCTAssertEqual(Route.parse(""), Route(pane: .general))
        XCTAssertEqual(Route.parse("   "), Route(pane: .general))
    }

    func testAPaneParsesToItself() {
        XCTAssertEqual(Route.parse("sources"), Route(pane: .sources))
        XCTAssertEqual(Route.parse("maintenance"), Route(pane: .maintenance))
    }

    func testLeadingHashAndSlashesAreIgnored() {
        XCTAssertEqual(Route.parse("#/processing"), Route(pane: .processing))
        XCTAssertEqual(Route.parse("/access/"), Route(pane: .access))
    }

    /// The v1 panes this build used to have.
    func testTheV1ValuesResolveToWhereTheyLiveNow() {
        XCTAssertEqual(Route.parse("data"), Route(pane: .storage))
        XCTAssertEqual(
            Route.parse("capture"), Route(pane: .sources, detail: "capture-defaults")
        )
        XCTAssertEqual(
            Route.parse("docproc"), Route(pane: .processing, detail: "docproc")
        )
        XCTAssertEqual(Route.parse("manage-sources"), Route(pane: .sources))
        XCTAssertEqual(
            Route.parse("local-decisions"), Route(pane: .processing, detail: "decisions")
        )
    }

    func testADetailAndItsEntityParse() {
        XCTAssertEqual(
            Route.parse("sources/file/f12"),
            Route(pane: .sources, detail: "file", ref: "f12")
        )
    }

    /// Falling back to the nearest valid parent, never to an empty window.
    func testUnknownValuesFallBackToTheNearestParent() {
        XCTAssertEqual(Route.parse("nonsense"), Route(pane: .general))
        XCTAssertEqual(Route.parse("sources/not-a-detail"), Route(pane: .sources))
        XCTAssertEqual(
            Route.parse("processing/docproc"), Route(pane: .processing, detail: "docproc")
        )
    }

    /// A detail of the wrong pane is not a detail.
    func testADetailOnlyBelongsToItsOwnPane() {
        XCTAssertEqual(Route.parse("sources/docproc"), Route(pane: .sources))
        XCTAssertEqual(Route.parse("processing/capture-defaults"), Route(pane: .processing))
    }

    func testPersistableRoundTrips() {
        for text in ["general", "sources", "processing/search", "sources/file/f12"] {
            XCTAssertEqual(Route.parse(text).persistable, text)
        }
    }

    // MARK: Migration

    private func store(_ value: String?) -> UserDefaults {
        let suite = UserDefaults(suiteName: "route-tests-\(UUID().uuidString)")!
        if let value { suite.set(value, forKey: SettingsWindowController.routeKey) }
        return suite
    }

    func testMigrationRewritesAStoredV1Value() {
        let defaults = store("capture")
        let route = SettingsWindowController.migrateStoredRoute(defaults)
        XCTAssertEqual(route, Route(pane: .sources, detail: "capture-defaults"))
        XCTAssertEqual(
            defaults.string(forKey: SettingsWindowController.routeKey),
            "sources/capture-defaults"
        )
        XCTAssertEqual(defaults.integer(forKey: SettingsWindowController.routeVersionKey), 2)
    }

    func testMigrationIsIdempotent() {
        let defaults = store("processing/docproc")
        let first = SettingsWindowController.migrateStoredRoute(defaults)
        let second = SettingsWindowController.migrateStoredRoute(defaults)
        XCTAssertEqual(first, second)
        XCTAssertEqual(
            defaults.string(forKey: SettingsWindowController.routeKey), "processing/docproc"
        )
    }

    func testAnUnreadableStoredValueMigratesToGeneral() {
        let defaults = store("a-pane-that-never-existed")
        XCTAssertEqual(
            SettingsWindowController.migrateStoredRoute(defaults), Route(pane: .general)
        )
        XCTAssertEqual(defaults.string(forKey: SettingsWindowController.routeKey), "general")
    }
}
