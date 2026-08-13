import Foundation

enum ServiceCondition: Equatable, Sendable {
    case checking
    /// No safe status to show: the adapter published none, or nothing usable
    /// came back at all. Either way the menu offers no controls.
    case unavailable(LifecycleRunnerError)
    case status(CoreSafeStatusV1)
}

enum MenuOperation: Equatable, Sendable {
    case idle
    case refreshing
    case starting
    case stopping
}

struct MenuSnapshot: Equatable, Sendable {
    let condition: ServiceCondition
    let operation: MenuOperation

    static let checking = MenuSnapshot(condition: .checking, operation: .idle)

    init(condition: ServiceCondition, operation: MenuOperation = .idle) {
        self.condition = condition
        self.operation = operation
    }

    /// Only the safe status is read. `outcome` and `code` are validated on the
    /// way in and then discarded: what a user sees is what the contract says a
    /// pre-authentication surface may see, and nothing else.
    init(document: LifecycleDocument) {
        if let safeStatus = document.safeStatus {
            self.init(condition: .status(safeStatus))
        } else {
            self.init(condition: .unavailable(.noSafeStatus))
        }
    }
}

struct MenuProjection: Equatable, Sendable {
    let title: String
    let detail: String
    let symbolName: String
    let refreshEnabled: Bool
    let startEnabled: Bool
    let stopEnabled: Bool

    static func project(_ snapshot: MenuSnapshot) -> MenuProjection {
        switch snapshot.operation {
        case .starting:
            return MenuProjection(
                title: "OmniVia Core — Starting…",
                detail: "Waiting for the Core lifecycle contract",
                symbolName: "arrow.triangle.2.circlepath",
                refreshEnabled: false,
                startEnabled: false,
                stopEnabled: false
            )
        case .stopping:
            return MenuProjection(
                title: "OmniVia Core — Stopping…",
                detail: "Waiting for the Core Service to unwind",
                symbolName: "stop.circle",
                refreshEnabled: false,
                startEnabled: false,
                stopEnabled: false
            )
        case .idle, .refreshing:
            break
        }

        let busy = snapshot.operation == .refreshing
        switch snapshot.condition {
        case .checking:
            return MenuProjection(
                title: "OmniVia Core — Checking…",
                detail: "Contacting the Core Service",
                symbolName: "circle.dotted",
                refreshEnabled: !busy,
                startEnabled: false,
                stopEnabled: false
            )
        case let .unavailable(reason):
            return MenuProjection(
                title: "OmniVia Core — Status Unavailable",
                detail: detail(for: reason),
                symbolName: "questionmark.circle",
                refreshEnabled: !busy,
                // No safe status means no permitted actions, and an action this
                // companion was not told it may take is one it does not offer.
                startEnabled: false,
                stopEnabled: false
            )
        case let .status(status):
            return MenuProjection(
                title: title(for: status),
                detail: detail(for: status),
                symbolName: symbolName(for: status),
                refreshEnabled: !busy,
                // Availability comes from `permitted_actions` alone: never from
                // the lifecycle phase, and never from a guess about the process.
                startEnabled: !busy && status.permittedActions.contains(.start),
                stopEnabled: !busy && status.permittedActions.contains(.stop)
            )
        }
    }

    private static func title(for status: CoreSafeStatusV1) -> String {
        let phase: String
        switch status.lifecycleState {
        case .starting:
            phase = "Starting…"
        case .running:
            phase = status.readinessState == .ready ? "Running" : "Not Ready"
        case .stopping:
            phase = "Stopping…"
        case .stopped:
            phase = "Stopped"
        case .failed:
            phase = "Needs Attention"
        case .unknown:
            phase = "Status Unknown"
        }
        return "OmniVia Core — " + phase
    }

    /// One fixed local phrase, chosen from the closed advisory set first and the
    /// closed connection/readiness states otherwise. Nothing the adapter wrote
    /// is ever echoed.
    private static func detail(for status: CoreSafeStatusV1) -> String {
        if let warning = CoreSafeWarningCode.allCases.first(where: status.warningCodes.contains) {
            switch warning {
            case .authenticationRequired:
                return "Core needs you to sign in"
            case .versionIncompatible:
                return "A different Core version owns this workspace"
            case .upgradeRequired:
                return "Core needs an update"
            case .workspaceFormatIncompatible:
                return "This workspace needs an upgrade"
            case .endpointUnreachable:
                return "Core is not answering"
            case .degraded:
                return "Core is running but not ready"
            }
        }
        switch status.connectionState {
        case .connected:
            return status.readinessState == .ready
                ? "Ready to serve requests"
                : "Core is running but not ready"
        case .connecting:
            return "Contacting the Core Service"
        case .disconnected:
            return "Service is not running"
        case .unreachable:
            return "Core is not answering"
        case .authenticationRequired:
            return "Core needs you to sign in"
        case .unknown:
            return "Core status is unknown"
        }
    }

    private static func detail(for reason: LifecycleRunnerError) -> String {
        switch reason {
        case .commandUnavailable:
            return "The Core command line tool is unavailable"
        case .timedOut:
            return "Core did not answer in time"
        case .noSafeStatus:
            return "Core published no status this companion may show"
        case .unreadableOutput, .actionMismatch, .exitStatusMismatch:
            return "Core published a status this companion cannot read"
        }
    }

    private static func symbolName(for status: CoreSafeStatusV1) -> String {
        switch status.lifecycleState {
        case .running:
            return status.readinessState == .ready
                ? "checkmark.circle.fill"
                : "exclamationmark.circle.fill"
        case .starting, .stopping:
            return "arrow.triangle.2.circlepath"
        case .stopped:
            return "circle"
        case .failed:
            return "exclamationmark.triangle.fill"
        case .unknown:
            return "questionmark.circle"
        }
    }
}
