import Foundation
import XCTest
@testable import OmniViaCoreStatusMenu

/// Lifecycle adapter v2 documents, assembled from parts so a test can make one
/// field malformed and leave the rest canonical.
enum Fixtures {
    static func target(
        contractVersion: String = "1.3",
        targetRef: String = "core-target-4f1e2c",
        displayName: String = "Local Core",
        kind: String = "local",
        workspaceRef: String = "ws-a74255f1-5631-497d",
        management: String = "locally_managed",
        endpointProfileRef: String = "core-endpoint-profile-9ab2",
        extra: String = ""
    ) -> String {
        """
        {"contract_version":"\(contractVersion)","target_ref":"\(targetRef)",\
        "display_name":"\(displayName)","kind":"\(kind)","workspace_ref":"\(workspaceRef)",\
        "management":"\(management)","endpoint_profile_ref":"\(endpointProfileRef)"\(extra)}
        """
    }

    static func status(
        contractVersion: String = "1.3",
        target: String = Fixtures.target(),
        lifecycle: String = "running",
        readiness: String = "ready",
        compatibility: String = "compatible",
        connection: String = "connected",
        warnings: String = "[]",
        actions: String = #"["stop"]"#,
        extra: String = ""
    ) -> String {
        """
        {"contract_version":"\(contractVersion)","target":\(target),\
        "lifecycle_state":"\(lifecycle)","readiness_state":"\(readiness)",\
        "compatibility_state":"\(compatibility)","connection_state":"\(connection)",\
        "warning_codes":\(warnings),"permitted_actions":\(actions)\(extra)}
        """
    }

    static func document(
        version: String = "2",
        action: String = "status",
        ok: String = "true",
        outcome: String = "running",
        code: String = "status_running",
        safeStatus: String? = Fixtures.status(),
        extra: String? = nil
    ) -> Data {
        var fields = [
            "\"lifecycle_adapter_version\":\(version)",
            "\"action\":\"\(action)\"",
            "\"ok\":\(ok)",
            "\"outcome\":\"\(outcome)\"",
            "\"code\":\"\(code)\"",
        ]
        if let safeStatus {
            fields.append("\"safe_status\":\(safeStatus)")
        }
        if let extra {
            fields.append(extra)
        }
        return Data(("{" + fields.joined(separator: ",") + "}").utf8)
    }

    /// A target this companion has no authority over: not on this host, and not
    /// this client's process to own.
    static let remoteTarget = Fixtures.target(kind: "private_remote")
    static let externalTarget = Fixtures.target(management: "externally_managed")
}

final class LifecycleContractTests: XCTestCase {
    func testDecodesAVersionTwoLocalStatus() throws {
        let document = try LifecycleDocument.decode(Fixtures.document())

        XCTAssertEqual(document.lifecycleAdapterVersion, 2)
        XCTAssertEqual(document.action, .status)
        XCTAssertEqual(document.outcome, .running)
        XCTAssertEqual(document.code, .statusRunning)

        let status = try XCTUnwrap(document.safeStatus)
        XCTAssertEqual(status.lifecycleState, .running)
        XCTAssertEqual(status.readinessState, .ready)
        XCTAssertEqual(status.compatibilityState, .compatible)
        XCTAssertEqual(status.connectionState, .connected)
        XCTAssertEqual(status.target.kind, .local)
        XCTAssertEqual(status.target.management, .locallyManaged)
        XCTAssertEqual(status.permittedActions, [.stop])
        XCTAssertTrue(status.warningCodes.isEmpty)
    }

    func testDecodesTheOptionalPublishedVersions() throws {
        let document = try LifecycleDocument.decode(
            Fixtures.document(
                safeStatus: Fixtures.status(
                    extra: #","server_version":"0.6.5-rc.1+build.4","protocol_version":"1.3""#
                )
            )
        )

        let status = try XCTUnwrap(document.safeStatus)
        XCTAssertEqual(status.serverVersion, "0.6.5-rc.1+build.4")
        XCTAssertEqual(status.protocolVersion, "1.3")
    }

    func testAcceptsADocumentWithNoSafeStatus() throws {
        let document = try LifecycleDocument.decode(
            Fixtures.document(
                ok: "false",
                outcome: "failed",
                code: "start_workspace_missing",
                safeStatus: nil
            ).replacingAction("start")
        )

        XCTAssertNil(document.safeStatus)
    }

    func testRejectsVersionOne() {
        let v1 = Data(
            #"{"lifecycle_adapter_version":1,"action":"status","ok":true,"outcome":"running","service":{"workspace_id":"ws-1","service_instance_id":"svc-1","state":"READY","ready":true,"unmet":[]},"reason":null}"#.utf8
        )

        XCTAssertThrowsError(try LifecycleDocument.decode(v1))
    }

    func testRejectsAnUnknownAdapterVersion() {
        for version in ["0", "3", "20"] {
            XCTAssertThrowsError(
                try LifecycleDocument.decode(Fixtures.document(version: version)),
                "adapter version \(version) must fail closed"
            )
        }
    }

    func testRejectsAMissingOrUnknownCode() {
        var withoutCode = Fixtures.document()
        withoutCode = Data(
            String(decoding: withoutCode, as: UTF8.self)
                .replacingOccurrences(of: #","code":"status_running""#, with: "")
                .utf8
        )

        XCTAssertThrowsError(try LifecycleDocument.decode(withoutCode))
        XCTAssertThrowsError(
            try LifecycleDocument.decode(Fixtures.document(code: "status_confused"))
        )
    }

    func testRejectsACodeThatBelongsToAnotherAction() {
        XCTAssertThrowsError(
            try LifecycleDocument.decode(Fixtures.document(code: "stop_stopped"))
        )
    }

    func testRejectsAnOutcomeThatBelongsToAnotherAction() {
        XCTAssertThrowsError(
            try LifecycleDocument.decode(
                Fixtures.document(outcome: "stopped", code: "status_not_running")
            )
        )
    }

    /// Every valid `(code, action, outcome, ok)` tuple the adapter may publish.
    /// The table is the contract, so it is asserted from both sides.
    func testAcceptsEveryValidCodeOutcomeTuple() throws {
        let valid = [
            ("start_started", "start", "started", "true"),
            ("start_attached", "start", "attached", "true"),
            ("start_workspace_missing", "start", "failed", "false"),
            ("start_incompatible_service", "start", "failed", "false"),
            ("start_timeout", "start", "failed", "false"),
            ("start_spawn_failed", "start", "failed", "false"),
            ("start_not_ready", "start", "failed", "false"),
            ("start_failed", "start", "failed", "false"),
            ("stop_stopped", "stop", "stopped", "true"),
            ("stop_not_running", "stop", "not_running", "true"),
            ("stop_service_unreachable", "stop", "failed", "false"),
            ("stop_no_process", "stop", "failed", "false"),
            ("stop_identity_mismatch", "stop", "failed", "false"),
            ("stop_timeout", "stop", "failed", "false"),
            ("stop_process_lingering", "stop", "failed", "false"),
            ("status_running", "status", "running", "true"),
            ("status_not_running", "status", "not_running", "false"),
            ("status_unreachable", "status", "failed", "false"),
            ("status_incompatible", "status", "failed", "false"),
            ("internal_error", "start", "failed", "false"),
            ("internal_error", "stop", "failed", "false"),
            ("internal_error", "status", "failed", "false"),
        ]

        for (code, action, outcome, ok) in valid {
            XCTAssertNoThrow(
                try LifecycleDocument.decode(
                    Fixtures.document(
                        action: action,
                        ok: ok,
                        outcome: outcome,
                        code: code,
                        safeStatus: nil
                    )
                ),
                "\(code) must decode as \(action)/\(outcome)/ok \(ok)"
            )
        }
    }

    /// Belonging to the right action is not enough: within one action, a code
    /// still names exactly one outcome and one `ok`.
    func testRejectsACodeAndOutcomeTheAdapterCannotPair() {
        let malformed = [
            ("status_not_running", "status", "failed", "false"),
            ("status_not_running", "status", "not_running", "true"),
            ("status_running", "status", "running", "false"),
            ("start_failed", "start", "started", "true"),
            ("start_failed", "start", "started", "false"),
            ("start_started", "start", "attached", "true"),
            ("start_attached", "start", "failed", "false"),
            ("stop_stopped", "stop", "failed", "false"),
            ("stop_stopped", "stop", "stopped", "false"),
            ("stop_not_running", "stop", "not_running", "false"),
            ("stop_timeout", "stop", "stopped", "true"),
            ("internal_error", "status", "not_running", "false"),
            ("internal_error", "start", "failed", "true"),
        ]

        for (code, action, outcome, ok) in malformed {
            XCTAssertThrowsError(
                try LifecycleDocument.decode(
                    Fixtures.document(
                        action: action,
                        ok: ok,
                        outcome: outcome,
                        code: code,
                        safeStatus: nil
                    )
                ),
                "\(code) as \(action)/\(outcome)/ok \(ok) must fail closed"
            )
        }
    }

    /// The canonical schemas are closed objects, at every level.
    func testRejectsAnUnknownKeyAtEveryObjectLevel() {
        let malformed = [
            Fixtures.document(extra: #""severity":"high""#),
            Fixtures.document(extra: #""stderr_tail":"/Users/someone/.omnivia is locked""#),
            Fixtures.document(safeStatus: Fixtures.status(extra: #","pid":4131"#)),
            Fixtures.document(safeStatus: Fixtures.status(extra: #","service_pid":4131"#)),
            Fixtures.document(
                safeStatus: Fixtures.status(
                    target: Fixtures.target(extra: #","endpoint":"http://127.0.0.1:8123""#)
                )
            ),
        ]

        for document in malformed {
            XCTAssertThrowsError(
                try LifecycleDocument.decode(document),
                "an unknown key must fail closed"
            )
        }
    }

    func testRejectsAFailedOutcomeThatClaimsSuccess() {
        XCTAssertThrowsError(
            try LifecycleDocument.decode(
                Fixtures.document(ok: "true", outcome: "failed", code: "internal_error")
            )
        )
    }

    func testRejectsUnknownClosedVocabularyValues() {
        let malformed = [
            Fixtures.status(lifecycle: "paused"),
            Fixtures.status(readiness: "probably"),
            Fixtures.status(compatibility: "newer"),
            Fixtures.status(connection: "half_open"),
            Fixtures.status(warnings: #"["disk_full"]"#),
            Fixtures.status(actions: #"["uninstall"]"#),
            Fixtures.status(target: Fixtures.target(kind: "edge")),
            Fixtures.status(target: Fixtures.target(management: "self_managed")),
        ]

        for status in malformed {
            XCTAssertThrowsError(
                try LifecycleDocument.decode(Fixtures.document(safeStatus: status)),
                "an unknown closed value must fail closed"
            )
        }
    }

    func testRejectsMalformedScalars() {
        let malformed = [
            Fixtures.status(target: Fixtures.target(targetRef: "-leading-punctuation")),
            Fixtures.status(target: Fixtures.target(targetRef: "has space")),
            Fixtures.status(target: Fixtures.target(targetRef: "")),
            Fixtures.status(target: Fixtures.target(targetRef: String(repeating: "a", count: 129))),
            Fixtures.status(target: Fixtures.target(displayName: "")),
            Fixtures.status(target: Fixtures.target(displayName: String(repeating: "n", count: 257))),
            Fixtures.status(target: Fixtures.target(workspaceRef: "ws 1")),
            Fixtures.status(target: Fixtures.target(endpointProfileRef: "profile/one")),
            Fixtures.status(extra: #","server_version":"1.2""#),
            Fixtures.status(extra: #","server_version":"01.2.3""#),
            Fixtures.status(extra: #","protocol_version":"1.3.0""#),
            Fixtures.status(extra: #","protocol_version":"1.x""#),
        ]

        for status in malformed {
            XCTAssertThrowsError(
                try LifecycleDocument.decode(Fixtures.document(safeStatus: status)),
                "a malformed scalar must fail closed"
            )
        }
    }

    func testRejectsDuplicateWarningCodesAndActions() {
        let duplicates = [
            Fixtures.status(
                readiness: "not_ready",
                warnings: #"["degraded","degraded"]"#,
                actions: "[]"
            ),
            Fixtures.status(actions: #"["stop","stop"]"#),
        ]

        for status in duplicates {
            XCTAssertThrowsError(
                try LifecycleDocument.decode(Fixtures.document(safeStatus: status)),
                "a repeated entry must fail closed"
            )
        }
    }

    func testRejectsContractVersionDisagreement() {
        XCTAssertThrowsError(
            try LifecycleDocument.decode(
                Fixtures.document(
                    safeStatus: Fixtures.status(
                        contractVersion: "1.4",
                        target: Fixtures.target(contractVersion: "1.3")
                    )
                )
            )
        )
    }

    /// The invariant is an exact match to the mirror this build carries, not any
    /// `1.x`: a document the two sides agree on is still one this build was not
    /// written to read.
    func testRejectsAnyContractVersionButTheOneThisBuildMirrors() {
        for version in ["1.2", "1.4", "2.0"] {
            XCTAssertThrowsError(
                try LifecycleDocument.decode(
                    Fixtures.document(
                        safeStatus: Fixtures.status(
                            contractVersion: version,
                            target: Fixtures.target(contractVersion: version)
                        )
                    )
                ),
                "contract version \(version) must fail closed"
            )
        }
    }

    func testRejectsProcessActionsForARemoteOrExternallyManagedTarget() {
        for target in [Fixtures.remoteTarget, Fixtures.externalTarget] {
            for actions in [#"["start"]"#, #"["stop"]"#, #"["restart"]"#] {
                XCTAssertThrowsError(
                    try LifecycleDocument.decode(
                        Fixtures.document(safeStatus: Fixtures.status(target: target, actions: actions))
                    ),
                    "a process action must fail closed for a target this client does not own"
                )
            }
        }
    }

    func testAcceptsARemoteTargetThatOffersNoProcessAction() throws {
        let document = try LifecycleDocument.decode(
            Fixtures.document(
                safeStatus: Fixtures.status(target: Fixtures.remoteTarget, actions: #"["reconnect"]"#)
            )
        )

        let status = try XCTUnwrap(document.safeStatus)
        XCTAssertEqual(status.permittedActions, [.reconnect])
    }

    /// Four documents as the CLI's own `encode_core_safe_status` renders them,
    /// kept verbatim: a producer-side change this mirror cannot read fails here
    /// rather than in a menu that quietly stops offering controls.
    func testDecodesDocumentsTheAdapterActuallyPublishes() throws {
        let published = [
            #"{"action": "status", "code": "status_running", "lifecycle_adapter_version": 2, "ok": true, "outcome": "running", "safe_status": {"compatibility_state": "compatible", "connection_state": "connected", "contract_version": "1.3", "lifecycle_state": "running", "permitted_actions": ["stop"], "protocol_version": "1.3", "readiness_state": "ready", "server_version": "0.6.5", "target": {"contract_version": "1.3", "display_name": "Local Core", "endpoint_profile_ref": "core-endpoint-profile-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "kind": "local", "management": "locally_managed", "target_ref": "core-target-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "workspace_ref": "ws-a74255f1-5631-497d"}, "warning_codes": []}}"#,
            #"{"action": "status", "code": "status_not_running", "lifecycle_adapter_version": 2, "ok": false, "outcome": "not_running", "safe_status": {"compatibility_state": "unknown", "connection_state": "disconnected", "contract_version": "1.3", "lifecycle_state": "stopped", "permitted_actions": ["start"], "readiness_state": "not_ready", "target": {"contract_version": "1.3", "display_name": "Local Core", "endpoint_profile_ref": "core-endpoint-profile-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "kind": "local", "management": "locally_managed", "target_ref": "core-target-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "workspace_ref": "ws-a74255f1-5631-497d"}, "warning_codes": []}}"#,
            #"{"action": "start", "code": "start_incompatible_service", "lifecycle_adapter_version": 2, "ok": false, "outcome": "failed", "safe_status": {"compatibility_state": "incompatible", "connection_state": "disconnected", "contract_version": "1.3", "lifecycle_state": "unknown", "permitted_actions": [], "readiness_state": "not_ready", "target": {"contract_version": "1.3", "display_name": "Local Core", "endpoint_profile_ref": "core-endpoint-profile-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "kind": "local", "management": "locally_managed", "target_ref": "core-target-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "workspace_ref": "ws-a74255f1-5631-497d"}, "warning_codes": ["version_incompatible"]}}"#,
            #"{"action": "stop", "code": "stop_service_unreachable", "lifecycle_adapter_version": 2, "ok": false, "outcome": "failed", "safe_status": {"compatibility_state": "unknown", "connection_state": "unreachable", "contract_version": "1.3", "lifecycle_state": "unknown", "permitted_actions": ["start"], "readiness_state": "not_ready", "target": {"contract_version": "1.3", "display_name": "Local Core", "endpoint_profile_ref": "core-endpoint-profile-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "kind": "local", "management": "locally_managed", "target_ref": "core-target-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "workspace_ref": "ws-a74255f1-5631-497d"}, "warning_codes": ["endpoint_unreachable"]}}"#,
        ]
        let expected: [(CoreLifecycleState, [CoreSafeAction])] = [
            (.running, [.stop]), (.stopped, [.start]), (.unknown, []), (.unknown, [.start]),
        ]

        for (json, expectation) in zip(published, expected) {
            let status = try XCTUnwrap(LifecycleDocument.decode(Data(json.utf8)).safeStatus)
            XCTAssertEqual(status.lifecycleState, expectation.0)
            XCTAssertEqual(status.permittedActions, expectation.1)
        }
    }

    // MARK: - Runner

    func testRunnerAcceptsNonzeroNotRunningWhenDocumentAgrees() throws {
        let result = CLICommandRunner.decode(
            action: .status,
            terminationStatus: 1,
            output: Fixtures.document(
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

        guard case let .success(document) = result else {
            return XCTFail("expected a valid not-running lifecycle document")
        }
        XCTAssertEqual(document.outcome, .notRunning)
        XCTAssertEqual(document.safeStatus?.permittedActions, [.start])
    }

    func testRunnerRejectsExitStatusDisagreement() {
        XCTAssertEqual(
            CLICommandRunner.decode(
                action: .status,
                terminationStatus: 1,
                output: Fixtures.document()
            ),
            .failure(.exitStatusMismatch)
        )
    }

    func testRunnerRejectsAResultForAnotherAction() {
        XCTAssertEqual(
            CLICommandRunner.decode(
                action: .stop,
                terminationStatus: 0,
                output: Fixtures.document()
            ),
            .failure(.actionMismatch)
        )
    }

    func testRunnerRefusesUnreadableOutputWithoutQuotingIt() {
        let result = CLICommandRunner.decode(
            action: .status,
            terminationStatus: 1,
            output: Data(#"{"lifecycle_adapter_version":2,"action":"status","reason":"/Users/someone/.omnivia is locked"}"#.utf8)
        )

        XCTAssertEqual(result, .failure(.unreadableOutput))
    }
}

private extension Data {
    /// Swap the fixture's action, for the one case that needs a `start` frame.
    func replacingAction(_ action: String) -> Data {
        Data(
            String(decoding: self, as: UTF8.self)
                .replacingOccurrences(of: #""action":"status""#, with: #""action":"\#(action)""#)
                .utf8
        )
    }
}
