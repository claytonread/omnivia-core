import Foundation
import XCTest
@testable import OmniViaCoreStatusMenu

final class LifecycleContractTests: XCTestCase {
    func testDecodesTheVersionedRunningSnapshot() throws {
        let data = Data(
            #"{"lifecycle_adapter_version":1,"action":"status","ok":true,"outcome":"running","service":{"workspace_id":"ws-1","service_instance_id":"svc-1","state":"READY","ready":true,"unmet":[]}}"#.utf8
        )

        let document = try LifecycleDocument.decode(data)

        XCTAssertEqual(document.lifecycleAdapterVersion, 1)
        XCTAssertEqual(document.action, .status)
        XCTAssertEqual(document.outcome, .running)
        XCTAssertEqual(
            document.service,
            ServiceSnapshot(
                workspaceID: "ws-1",
                serviceInstanceID: "svc-1",
                state: "READY",
                ready: true,
                unmet: []
            )
        )
    }

    func testRejectsAnUnsupportedVersion() {
        let data = Data(
            #"{"lifecycle_adapter_version":2,"action":"status","ok":false,"outcome":"not_running"}"#.utf8
        )

        XCTAssertThrowsError(try LifecycleDocument.decode(data))
    }

    func testRejectsAnOutcomeFromAnotherAction() {
        let data = Data(
            #"{"lifecycle_adapter_version":1,"action":"stop","ok":true,"outcome":"running","service":{"workspace_id":"ws-1","service_instance_id":"svc-1","state":"READY","ready":true,"unmet":[]}}"#.utf8
        )

        XCTAssertThrowsError(try LifecycleDocument.decode(data))
    }

    func testRejectsRunningWithoutAServiceSnapshot() {
        let data = Data(
            #"{"lifecycle_adapter_version":1,"action":"status","ok":true,"outcome":"running"}"#.utf8
        )

        XCTAssertThrowsError(try LifecycleDocument.decode(data))
    }

    func testRunnerAcceptsNonzeroNotRunningWhenDocumentAgrees() {
        let data = Data(
            #"{"lifecycle_adapter_version":1,"action":"status","ok":false,"outcome":"not_running"}"#.utf8
        )

        let result = CLICommandRunner.decode(
            action: .status,
            terminationStatus: 1,
            output: data,
            errorOutput: Data()
        )

        guard case let .success(document) = result else {
            return XCTFail("expected a valid not-running lifecycle document")
        }
        XCTAssertEqual(document.outcome, .notRunning)
    }

    func testRunnerRejectsExitStatusDisagreement() {
        let data = Data(
            #"{"lifecycle_adapter_version":1,"action":"stop","ok":true,"outcome":"stopped"}"#.utf8
        )

        XCTAssertEqual(
            CLICommandRunner.decode(
                action: .stop,
                terminationStatus: 1,
                output: data,
                errorOutput: Data()
            ),
            .failure(.exitStatusMismatch)
        )
    }
}
