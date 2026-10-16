import XCTest
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
}
