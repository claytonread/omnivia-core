import Foundation
import XCTest
@testable import OmniViaCoreStatusMenu

final class MenuProjectionTests: XCTestCase {
    private func status(
        target: String = Fixtures.target(),
        lifecycle: String = "running",
        readiness: String = "ready",
        compatibility: String = "compatible",
        connection: String = "connected",
        warnings: String = "[]",
        actions: String = #"["stop"]"#
    ) throws -> CoreSafeStatusV1 {
        let document = try LifecycleDocument.decode(
            Fixtures.document(
                safeStatus: Fixtures.status(
                    target: target,
                    lifecycle: lifecycle,
                    readiness: readiness,
                    compatibility: compatibility,
                    connection: connection,
                    warnings: warnings,
                    actions: actions
                )
            )
        )
        return try XCTUnwrap(document.safeStatus)
    }

    private func project(_ status: CoreSafeStatusV1) -> MenuProjection {
        MenuProjection.project(MenuSnapshot(condition: .status(status)))
    }

    func testCheckingNeverPretendsTheServiceIsStopped() {
        let projection = MenuProjection.project(.checking)

        XCTAssertEqual(projection.title, "OmniVia Core — Checking…")
        XCTAssertFalse(projection.startEnabled)
        XCTAssertFalse(projection.stopEnabled)
    }

    func testControlsComeOnlyFromPermittedActions() throws {
        let running = project(try status())
        XCTAssertEqual(running.title, "OmniVia Core — Running")
        XCTAssertEqual(running.detail, "Ready to serve requests")
        XCTAssertFalse(running.startEnabled)
        XCTAssertTrue(running.stopEnabled)

        let stopped = project(
            try status(
                lifecycle: "stopped",
                readiness: "not_ready",
                compatibility: "unknown",
                connection: "disconnected",
                actions: #"["start"]"#
            )
        )
        XCTAssertEqual(stopped.title, "OmniVia Core — Stopped")
        XCTAssertEqual(stopped.detail, "Service is not running")
        XCTAssertTrue(stopped.startEnabled)
        XCTAssertFalse(stopped.stopEnabled)
    }

    /// A running service that offers no action gets no control: the lifecycle
    /// phase is never a second opinion about what the caller may do.
    func testARunningStatusWithNoPermittedActionsOffersNoControls() throws {
        let projection = project(
            try status(readiness: "not_ready", warnings: #"["degraded"]"#, actions: "[]")
        )

        XCTAssertEqual(projection.title, "OmniVia Core — Not Ready")
        XCTAssertEqual(projection.detail, "Core is running but not ready")
        XCTAssertFalse(projection.startEnabled)
        XCTAssertFalse(projection.stopEnabled)
    }

    func testARemoteTargetNeverExposesLocalProcessControls() throws {
        for target in [Fixtures.remoteTarget, Fixtures.externalTarget] {
            let projection = project(try status(target: target, actions: #"["reconnect","open"]"#))

            XCTAssertFalse(projection.startEnabled)
            XCTAssertFalse(projection.stopEnabled)
        }
    }

    /// `restart` is a contract action this companion does not implement and
    /// never renders: a status offering it still moves no control.
    func testRestartIsNeverPromotedToAControl() throws {
        let projection = project(try status(actions: #"["restart"]"#))

        XCTAssertFalse(projection.startEnabled)
        XCTAssertFalse(projection.stopEnabled)
    }

    func testFailedAndAuthenticationRequiredUseFixedLocalPhrases() throws {
        let failed = project(
            try status(
                lifecycle: "failed",
                readiness: "not_ready",
                compatibility: "unknown",
                connection: "unreachable",
                warnings: #"["endpoint_unreachable"]"#,
                actions: #"["start"]"#
            )
        )
        XCTAssertEqual(failed.title, "OmniVia Core — Needs Attention")
        XCTAssertEqual(failed.detail, "Core is not answering")
        XCTAssertTrue(failed.startEnabled)
        XCTAssertFalse(failed.stopEnabled)

        let authenticating = project(
            try status(
                lifecycle: "unknown",
                readiness: "unknown",
                compatibility: "unknown",
                connection: "authentication_required",
                warnings: #"["authentication_required"]"#,
                actions: "[]"
            )
        )
        XCTAssertEqual(authenticating.title, "OmniVia Core — Status Unknown")
        XCTAssertEqual(authenticating.detail, "Core needs you to sign in")
        XCTAssertFalse(authenticating.startEnabled)
        XCTAssertFalse(authenticating.stopEnabled)
    }

    func testIncompatibleOwnershipIsStatedWithoutNamingTheOwner() throws {
        let projection = project(
            try status(
                lifecycle: "unknown",
                readiness: "not_ready",
                compatibility: "incompatible",
                connection: "disconnected",
                warnings: #"["version_incompatible"]"#,
                actions: "[]"
            )
        )

        XCTAssertEqual(projection.detail, "A different Core version owns this workspace")
        XCTAssertFalse(projection.startEnabled)
        XCTAssertFalse(projection.stopEnabled)
    }

    func testAnAbsentSafeStatusOffersNoControls() throws {
        let document = try LifecycleDocument.decode(Fixtures.document(safeStatus: nil))
        let projection = MenuProjection.project(MenuSnapshot(document: document))

        XCTAssertEqual(projection.title, "OmniVia Core — Status Unavailable")
        XCTAssertEqual(projection.detail, "Core published no status this companion may show")
        XCTAssertFalse(projection.startEnabled)
        XCTAssertFalse(projection.stopEnabled)
    }

    func testTransitionsDisableConflictingActions() throws {
        let starting = MenuProjection.project(
            MenuSnapshot(condition: .status(try status()), operation: .starting)
        )
        let stopping = MenuProjection.project(
            MenuSnapshot(condition: .status(try status()), operation: .stopping)
        )

        XCTAssertEqual(starting.title, "OmniVia Core — Starting…")
        XCTAssertFalse(starting.refreshEnabled)
        XCTAssertFalse(starting.startEnabled)
        XCTAssertFalse(starting.stopEnabled)
        XCTAssertEqual(stopping.title, "OmniVia Core — Stopping…")
        XCTAssertFalse(stopping.startEnabled)
        XCTAssertFalse(stopping.stopEnabled)
    }

    func testEveryRunnerFailureRendersAFixedPhraseAndNoControls() {
        let failures: [LifecycleRunnerError] = [
            .commandUnavailable, .timedOut, .unreadableOutput, .actionMismatch,
            .exitStatusMismatch, .noSafeStatus,
        ]
        let allowed: Set<String> = [
            "The Core command line tool is unavailable",
            "Core did not answer in time",
            "Core published a status this companion cannot read",
            "Core published no status this companion may show",
        ]

        for failure in failures {
            let projection = MenuProjection.project(
                MenuSnapshot(condition: .unavailable(failure))
            )
            XCTAssertTrue(allowed.contains(projection.detail), projection.detail)
            XCTAssertFalse(projection.startEnabled)
            XCTAssertFalse(projection.stopEnabled)
        }
    }
}
