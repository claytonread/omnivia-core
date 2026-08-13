import Foundation
import XCTest
@testable import OmniViaCoreStatusMenu

final class MenuProjectionTests: XCTestCase {
    private let service = ServiceSnapshot(
        workspaceID: "ws-1",
        serviceInstanceID: "svc-1",
        state: "READY",
        ready: true,
        unmet: []
    )

    func testCheckingNeverPretendsTheServiceIsStopped() {
        let projection = MenuProjection.project(.checking)

        XCTAssertEqual(projection.title, "OmniVia Core — Checking…")
        XCTAssertFalse(projection.startEnabled)
        XCTAssertFalse(projection.stopEnabled)
    }

    func testRunningUsesOneServiceSnapshotForFactsAndActions() {
        let projection = MenuProjection.project(
            MenuSnapshot(condition: .running(service))
        )

        XCTAssertEqual(projection.title, "OmniVia Core — Running")
        XCTAssertTrue(projection.detail.contains("ws-1"))
        XCTAssertFalse(projection.startEnabled)
        XCTAssertTrue(projection.stopEnabled)
    }

    func testStoppedKeepsBothActionsInStablePositionsButEnablesOnlyStart() {
        let projection = MenuProjection.project(MenuSnapshot(condition: .stopped))

        XCTAssertEqual(projection.title, "OmniVia Core — Stopped")
        XCTAssertTrue(projection.startEnabled)
        XCTAssertFalse(projection.stopEnabled)
    }

    func testTransitionsDisableConflictingActions() {
        let starting = MenuProjection.project(
            MenuSnapshot(condition: .stopped, operation: .starting)
        )
        let stopping = MenuProjection.project(
            MenuSnapshot(condition: .running(service), operation: .stopping)
        )

        XCTAssertEqual(starting.title, "OmniVia Core — Starting…")
        XCTAssertFalse(starting.refreshEnabled)
        XCTAssertFalse(starting.startEnabled)
        XCTAssertFalse(starting.stopEnabled)
        XCTAssertEqual(stopping.title, "OmniVia Core — Stopping…")
        XCTAssertFalse(stopping.startEnabled)
        XCTAssertFalse(stopping.stopEnabled)
    }

    func testFailureIsBoundedAndKeepsSafetyCheckedRecoveryAvailable() {
        let longReason = String(repeating: "failure ", count: 80)
        let projection = MenuProjection.project(
            MenuSnapshot(condition: .failed(longReason))
        )

        XCTAssertEqual(projection.title, "OmniVia Core — Needs Attention")
        XCTAssertLessThanOrEqual(projection.detail.count, 181)
        XCTAssertTrue(projection.startEnabled)
        XCTAssertTrue(projection.stopEnabled)
    }
}
