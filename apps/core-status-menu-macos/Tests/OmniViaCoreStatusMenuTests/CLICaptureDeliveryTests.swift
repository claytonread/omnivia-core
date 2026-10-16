import XCTest
import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

final class CLICaptureDeliveryTests: XCTestCase {
    func testExplicitUnestablishedOutcomeRemainsUnknown() {
        let outcome = CLICaptureDelivery.outcome(
            from: [
                "error": [
                    "code": "internal_non_recoverable",
                    "details": ["outcome_established": false],
                ],
            ],
            terminationStatus: 1
        )

        XCTAssertEqual(outcome, .unknown)
    }

    func testOrdinaryIdempotencyConflictIsADefiniteFailure() {
        let outcome = CLICaptureDelivery.outcome(
            from: ["error": ["code": "idempotency_conflict"]],
            terminationStatus: 1
        )

        XCTAssertEqual(outcome, .definiteFailure(code: "idempotency_conflict"))
    }

    /// The capture itself must be incapable of starting Core: the CLI mode is
    /// global, so it precedes the command, and the command is otherwise unchanged.
    func testTheCaptureCommandAttachesOnlyAndNeverStartsCore() {
        let delivery = CLICaptureDelivery(
            executable: URL(fileURLWithPath: "/omnivia"),
            installationState: URL(fileURLWithPath: "/state"),
            deliveryBinding: binding
        )
        XCTAssertEqual(
            delivery.captureArguments(payloadURL: URL(fileURLWithPath: "/payload"), operationKey: "k-1"),
            [
                "--require-running",
                "--installation-state", "/state",
                "--workspace-id", "ws-a74255f1-5631-497d",
                "--timeout-ms", "20000",
                "document", "capture",
                "--input-stdin",
                "--payload-file", "/payload",
                "--idempotency-key", "k-1",
                "--json",
            ]
        )
    }

    // MARK: - The pre-send lifecycle gate

    private let binding = CaptureDeliveryBinding.companion(
        workspaceRef: "ws-a74255f1-5631-497d",
        targetRef: "core-target-4f1e2c"
    )

    private func gate(_ data: Data) -> CaptureDeliveryOutcome? {
        CLICaptureDelivery.gate(Result { try LifecycleDocument.decode(data) }.mapError { _ in .unreadableOutput },
                                binding: binding)
    }

    func testOnlyARunningServiceForTheFrozenTargetIsSentTo() {
        XCTAssertNil(gate(Fixtures.document()))
        // A running service that is another target or workspace is not a fallback.
        XCTAssertEqual(
            gate(Fixtures.document(safeStatus: Fixtures.status(
                target: Fixtures.target(targetRef: "core-target-other")
            ))),
            .notSent
        )
        XCTAssertEqual(
            gate(Fixtures.document(safeStatus: Fixtures.status(
                target: Fixtures.target(workspaceRef: "ws-other")
            ))),
            .notSent
        )
        XCTAssertEqual(gate(Fixtures.document(safeStatus: nil)), .notSent)
    }

    func testAStoppedUnreachableOrUnreadableServiceIsNotSentTo() {
        XCTAssertEqual(
            gate(Fixtures.document(
                ok: "false", outcome: "not_running", code: "status_not_running",
                safeStatus: Fixtures.status(lifecycle: "stopped", connection: "disconnected", actions: #"["start"]"#)
            )),
            .notSent
        )
        XCTAssertEqual(
            gate(Fixtures.document(ok: "false", outcome: "failed", code: "status_unreachable")),
            .notSent
        )
        XCTAssertEqual(
            gate(Fixtures.document(ok: "false", outcome: "failed", code: "internal_error")),
            .notSent
        )
        XCTAssertEqual(gate(Data("not json".utf8)), .notSent)
        // Another action's document is not a status answer either.
        XCTAssertEqual(gate(Fixtures.document(action: "start", outcome: "started", code: "start_started")), .notSent)
    }

    func testAnIncompatibleServiceHoldsTheItem() {
        XCTAssertEqual(
            gate(Fixtures.document(ok: "false", outcome: "failed", code: "status_incompatible")),
            .held(.updateRequired)
        )
    }
}
