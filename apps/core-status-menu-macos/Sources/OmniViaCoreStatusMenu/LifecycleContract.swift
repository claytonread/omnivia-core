import Foundation

/// Version 1 published a raw `service` object and a free-form `reason`; both are
/// gone, and a version-1 document is refused rather than read.
let supportedLifecycleAdapterVersion = 2

enum LifecycleAction: String, Decodable, CaseIterable, Sendable {
    case start
    case stop
    case status
}

enum LifecycleOutcome: String, Decodable, CaseIterable, Sendable {
    case started
    case attached
    case running
    case notRunning = "not_running"
    case stopped
    case failed
}

/// The adapter's closed `code` set (`LIFECYCLE_CODES` in the CLI). This is the
/// whole of what the companion learns about *why*, and it is never displayed as
/// itself — the menu's words come from the safe status.
enum LifecycleCode: String, Decodable, CaseIterable, Sendable {
    case startStarted = "start_started"
    case startAttached = "start_attached"
    case startWorkspaceMissing = "start_workspace_missing"
    case startIncompatibleService = "start_incompatible_service"
    case startTimeout = "start_timeout"
    case startSpawnFailed = "start_spawn_failed"
    case startNotReady = "start_not_ready"
    case startFailed = "start_failed"
    case stopStopped = "stop_stopped"
    case stopNotRunning = "stop_not_running"
    case stopServiceUnreachable = "stop_service_unreachable"
    case stopNoProcess = "stop_no_process"
    case stopIdentityMismatch = "stop_identity_mismatch"
    case stopTimeout = "stop_timeout"
    case stopProcessLingering = "stop_process_lingering"
    case statusRunning = "status_running"
    case statusNotRunning = "status_not_running"
    case statusUnreachable = "status_unreachable"
    case statusIncompatible = "status_incompatible"
    case internalError = "internal_error"

    /// The one frame this code may appear in: the action it belongs to, the one
    /// outcome it may carry, and whether that frame reports success. A code that
    /// merely belongs to the right action is not enough — a `status_not_running`
    /// carrying `failed`, or a `stop_stopped` carrying `ok: false`, is a document
    /// disagreeing with itself, and the menu reads no such document.
    ///
    /// `internal_error` is the one code with no action of its own: every action
    /// may publish it, and only as a failure.
    var frame: (action: LifecycleAction?, outcome: LifecycleOutcome, ok: Bool) {
        switch self {
        case .startStarted:
            return (.start, .started, true)
        case .startAttached:
            return (.start, .attached, true)
        case .startWorkspaceMissing, .startIncompatibleService, .startTimeout,
             .startSpawnFailed, .startNotReady, .startFailed:
            return (.start, .failed, false)
        case .stopStopped:
            return (.stop, .stopped, true)
        case .stopNotRunning:
            return (.stop, .notRunning, true)
        case .stopServiceUnreachable, .stopNoProcess, .stopIdentityMismatch,
             .stopTimeout, .stopProcessLingering:
            return (.stop, .failed, false)
        case .statusRunning:
            return (.status, .running, true)
        case .statusNotRunning:
            return (.status, .notRunning, false)
        case .statusUnreachable, .statusIncompatible:
            return (.status, .failed, false)
        case .internalError:
            return (nil, .failed, false)
        }
    }
}

struct LifecycleDocument: Decodable, Equatable, Sendable {
    let lifecycleAdapterVersion: Int
    let action: LifecycleAction
    let ok: Bool
    let outcome: LifecycleOutcome
    let code: LifecycleCode
    /// Absent when the adapter could form no valid target, and absent rather
    /// than invalid when the contract would refuse it. Absence means the menu
    /// offers no controls at all.
    let safeStatus: CoreSafeStatusV1?

    /// Wire keys are snake_case; the decoder converts them, so these are the
    /// converted names.
    enum CodingKeys: String, CodingKey, CaseIterable {
        case lifecycleAdapterVersion
        case action
        case ok
        case outcome
        case code
        case safeStatus
    }

    init(from decoder: Decoder) throws {
        try decoder.rejectUnknownKeys(CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        lifecycleAdapterVersion = try container.decode(Int.self, forKey: .lifecycleAdapterVersion)
        guard lifecycleAdapterVersion == supportedLifecycleAdapterVersion else {
            throw CoreContractViolation()
        }

        action = try container.decode(LifecycleAction.self, forKey: .action)
        ok = try container.decode(Bool.self, forKey: .ok)
        outcome = try container.decode(LifecycleOutcome.self, forKey: .outcome)
        code = try container.decode(LifecycleCode.self, forKey: .code)
        safeStatus = try container.decodeIfPresent(CoreSafeStatusV1.self, forKey: .safeStatus)
        try safeStatus?.validate()

        let frame = code.frame
        guard frame.action.map({ $0 == action }) ?? true,
              frame.outcome == outcome,
              frame.ok == ok
        else { throw CoreContractViolation() }
    }

    static func decode(_ data: Data) throws -> LifecycleDocument {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(LifecycleDocument.self, from: data)
    }
}
