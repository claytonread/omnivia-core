import Foundation
import XCTest
@testable import OmniViaCoreStatusMenu

private final class FakeLifecycleRunner: LifecycleRunning, @unchecked Sendable {
    struct Request {
        let action: LifecycleAction
        let completion: LifecycleCompletion
    }

    private(set) var requests: [Request] = []

    func run(
        _ action: LifecycleAction,
        completion: @escaping LifecycleCompletion
    ) {
        requests.append(Request(action: action, completion: completion))
    }
}

final class LifecycleCoordinatorTests: XCTestCase {
    private func document(_ data: Data) throws -> LifecycleDocument {
        try LifecycleDocument.decode(data)
    }

    func testSingleFlightDoesNotOverlapRefreshAndMutation() {
        let runner = FakeLifecycleRunner()
        let coordinator = LifecycleCoordinator(
            runner: runner,
            schedulePostMutationRefresh: { _ in }
        )

        coordinator.refresh()
        coordinator.start()
        coordinator.stop()

        XCTAssertEqual(runner.requests.map(\.action), [.status])
        XCTAssertTrue(coordinator.inFlight)
        XCTAssertEqual(coordinator.snapshot.operation, .refreshing)
    }

    func testLateDuplicatePollCannotOverwriteANewerTransition() throws {
        let runner = FakeLifecycleRunner()
        let coordinator = LifecycleCoordinator(
            runner: runner,
            schedulePostMutationRefresh: { _ in }
        )
        let stopped = try document(
            Fixtures.document(
                ok: "false",
                outcome: "not_running",
                code: "status_not_running",
                safeStatus: Fixtures.status(
                    lifecycle: "stopped",
                    readiness: "not_ready",
                    compatibility: "unknown",
                    connection: "disconnected",
                    actions: #"["start"]"#
                )
            )
        )
        let started = try document(Fixtures.document())

        coordinator.refresh()
        let oldCompletion = runner.requests[0].completion
        oldCompletion(.success(stopped))
        coordinator.start()
        XCTAssertEqual(coordinator.snapshot.operation, .starting)

        oldCompletion(.success(stopped))
        XCTAssertEqual(coordinator.snapshot.operation, .starting)

        runner.requests[1].completion(.success(started))
        guard case let .status(status) = coordinator.snapshot.condition else {
            return XCTFail("expected the start result to win")
        }
        XCTAssertEqual(status.lifecycleState, .running)
        XCTAssertEqual(coordinator.snapshot.operation, .idle)
    }

    func testRunnerFailureLeavesNoStatusAndNoControls() {
        let runner = FakeLifecycleRunner()
        let coordinator = LifecycleCoordinator(
            runner: runner,
            schedulePostMutationRefresh: { _ in }
        )

        coordinator.refresh()
        runner.requests[0].completion(.failure(.timedOut))

        XCTAssertEqual(coordinator.snapshot.condition, .unavailable(.timedOut))
        let projection = MenuProjection.project(coordinator.snapshot)
        XCTAssertEqual(projection.detail, "Core did not answer in time")
        XCTAssertFalse(projection.startEnabled)
        XCTAssertFalse(projection.stopEnabled)
        XCTAssertFalse(coordinator.inFlight)
    }

    /// Quitting the companion must leave Core running: the terminate path the
    /// app delegate calls issues no lifecycle command at all.
    func testQuittingTheCompanionIssuesNoLifecycleCommand() throws {
        let runner = FakeLifecycleRunner()
        let coordinator = LifecycleCoordinator(
            runner: runner,
            schedulePostMutationRefresh: { _ in }
        )

        coordinator.refresh()
        runner.requests[0].completion(.success(try document(Fixtures.document())))
        coordinator.companionWillTerminate()

        XCTAssertEqual(runner.requests.map(\.action), [.status])
        guard case .status = coordinator.snapshot.condition else {
            return XCTFail("expected the running status to survive the quit")
        }
    }

    /// The adapter's human stderr is drained so it cannot deadlock the pipe, and
    /// then dropped: none of it, and no launcher or decoder message, reaches the
    /// rendered menu.
    func testAdapterStderrNeverReachesTheMenu() throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = directory.appendingPathComponent("omnivia")
        try """
        #!/bin/sh
        echo 'not a lifecycle document'
        echo 'traceback: /Users/someone/.omnivia/run/service.sock token=hunter2 pid=4242' >&2
        exit 1
        """.write(to: executable, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: executable.path
        )

        let runner = CLICommandRunner(
            executable: executable,
            installationState: URL(fileURLWithPath: "/tmp/installation-state"),
            workspaceID: "ws-test"
        )
        let coordinator = LifecycleCoordinator(
            runner: runner,
            schedulePostMutationRefresh: { _ in }
        )
        let rendered = expectation(description: "the failure is rendered")
        coordinator.onChange = { snapshot in
            if case .unavailable = snapshot.condition { rendered.fulfill() }
        }

        coordinator.refresh()
        wait(for: [rendered], timeout: 20)

        let projection = MenuProjection.project(coordinator.snapshot)
        for leak in ["hunter2", "4242", "/Users/someone", "traceback", "not a lifecycle document"] {
            XCTAssertFalse(projection.title.contains(leak))
            XCTAssertFalse(projection.detail.contains(leak))
        }
        XCTAssertEqual(projection.detail, "Core published a status this companion cannot read")
    }
}
