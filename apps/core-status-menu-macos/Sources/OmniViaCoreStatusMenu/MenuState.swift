import Foundation

enum ServiceCondition: Equatable, Sendable {
    case checking
    case running(ServiceSnapshot)
    case stopped
    case failed(String)
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

    init(document: LifecycleDocument) {
        switch document.outcome {
        case .started, .attached, .running:
            if let service = document.service {
                self.init(condition: .running(service))
            } else {
                self.init(condition: .failed("Core returned no service snapshot"))
            }
        case .notRunning, .stopped:
            self.init(condition: .stopped)
        case .failed:
            self.init(
                condition: .failed(
                    MenuProjection.displayReason(document.reason ?? "Core lifecycle command failed")
                )
            )
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
        case let .running(service):
            let unmet = service.unmet.isEmpty ? "" : " · \(service.unmet.joined(separator: ", "))"
            return MenuProjection(
                title: service.ready ? "OmniVia Core — Running" : "OmniVia Core — Not Ready",
                detail: "\(service.workspaceID) · \(service.state)\(unmet)",
                symbolName: service.ready ? "checkmark.circle.fill" : "exclamationmark.circle.fill",
                refreshEnabled: !busy,
                startEnabled: false,
                stopEnabled: !busy
            )
        case .stopped:
            return MenuProjection(
                title: "OmniVia Core — Stopped",
                detail: "Service is not running",
                symbolName: "circle",
                refreshEnabled: !busy,
                startEnabled: !busy,
                stopEnabled: false
            )
        case let .failed(reason):
            return MenuProjection(
                title: "OmniVia Core — Needs Attention",
                detail: displayReason(reason),
                symbolName: "exclamationmark.triangle.fill",
                refreshEnabled: !busy,
                // Both lifecycle commands are safety-checked and idempotent at
                // the CLI boundary. Keeping them available gives a stale or
                // degraded service a recovery path without guessing its pid.
                startEnabled: !busy,
                stopEnabled: !busy
            )
        }
    }

    static func displayReason(_ reason: String) -> String {
        let oneLine = reason
            .split(whereSeparator: \.isNewline)
            .joined(separator: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !oneLine.isEmpty else { return "Core lifecycle command failed" }
        let prefix = String(oneLine.prefix(180))
        return prefix.count == oneLine.count ? prefix : prefix + "…"
    }
}
