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
    private func document(_ json: String) throws -> LifecycleDocument {
        try LifecycleDocument.decode(Data(json.utf8))
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
            #"{"lifecycle_adapter_version":1,"action":"status","ok":false,"outcome":"not_running"}"#
        )
        let started = try document(
            #"{"lifecycle_adapter_version":1,"action":"start","ok":true,"outcome":"started","service":{"workspace_id":"ws-1","service_instance_id":"svc-1","state":"READY","ready":true,"unmet":[]}}"#
        )

        coordinator.refresh()
        let oldCompletion = runner.requests[0].completion
        oldCompletion(.success(stopped))
        coordinator.start()
        XCTAssertEqual(coordinator.snapshot.operation, .starting)

        oldCompletion(.success(stopped))
        XCTAssertEqual(coordinator.snapshot.operation, .starting)

        runner.requests[1].completion(.success(started))
        guard case .running = coordinator.snapshot.condition else {
            return XCTFail("expected the start result to win")
        }
        XCTAssertEqual(coordinator.snapshot.operation, .idle)
    }

    func testRunnerFailureBecomesAVisibleErrorSnapshot() {
        let runner = FakeLifecycleRunner()
        let coordinator = LifecycleCoordinator(
            runner: runner,
            schedulePostMutationRefresh: { _ in }
        )

        coordinator.refresh()
        runner.requests[0].completion(.failure(.timedOut(.status)))

        guard case let .failed(reason) = coordinator.snapshot.condition else {
            return XCTFail("expected a failure snapshot")
        }
        XCTAssertTrue(reason.contains("timed out"))
        XCTAssertFalse(coordinator.inFlight)
    }
}
