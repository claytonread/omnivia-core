import Foundation

let supportedLifecycleAdapterVersion = 1

enum LifecycleAction: String, Codable, CaseIterable, Sendable {
    case start
    case stop
    case status
}

enum LifecycleOutcome: String, Codable, CaseIterable, Sendable {
    case started
    case attached
    case running
    case notRunning = "not_running"
    case stopped
    case failed
}

struct ServiceSnapshot: Codable, Equatable, Sendable {
    let workspaceID: String
    let serviceInstanceID: String
    let state: String
    let ready: Bool
    let unmet: [String]

    enum CodingKeys: String, CodingKey {
        case workspaceID = "workspace_id"
        case serviceInstanceID = "service_instance_id"
        case state
        case ready
        case unmet
    }
}

struct LifecycleDocument: Decodable, Equatable, Sendable {
    let lifecycleAdapterVersion: Int
    let action: LifecycleAction
    let ok: Bool
    let outcome: LifecycleOutcome
    let service: ServiceSnapshot?
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case lifecycleAdapterVersion = "lifecycle_adapter_version"
        case action
        case ok
        case outcome
        case service
        case reason
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        lifecycleAdapterVersion = try container.decode(
            Int.self, forKey: .lifecycleAdapterVersion
        )
        guard lifecycleAdapterVersion == supportedLifecycleAdapterVersion else {
            throw DecodingError.dataCorruptedError(
                forKey: .lifecycleAdapterVersion,
                in: container,
                debugDescription: "unsupported lifecycle adapter version"
            )
        }

        action = try container.decode(LifecycleAction.self, forKey: .action)
        ok = try container.decode(Bool.self, forKey: .ok)
        outcome = try container.decode(LifecycleOutcome.self, forKey: .outcome)
        service = try container.decodeIfPresent(ServiceSnapshot.self, forKey: .service)
        reason = try container.decodeIfPresent(String.self, forKey: .reason)

        let allowed: Set<LifecycleOutcome>
        switch action {
        case .start:
            allowed = [.started, .attached, .failed]
        case .stop:
            allowed = [.stopped, .notRunning, .failed]
        case .status:
            allowed = [.running, .notRunning, .failed]
        }
        guard allowed.contains(outcome) else {
            throw DecodingError.dataCorruptedError(
                forKey: .outcome,
                in: container,
                debugDescription: "outcome does not belong to lifecycle action"
            )
        }

        let requiresService = outcome == .started || outcome == .attached || outcome == .running
        guard !requiresService || service != nil else {
            throw DecodingError.dataCorruptedError(
                forKey: .service,
                in: container,
                debugDescription: "successful running outcomes require a service snapshot"
            )
        }
        guard outcome != .failed || !ok else {
            throw DecodingError.dataCorruptedError(
                forKey: .ok,
                in: container,
                debugDescription: "failed outcomes cannot be successful"
            )
        }
        guard !requiresService || ok else {
            throw DecodingError.dataCorruptedError(
                forKey: .ok,
                in: container,
                debugDescription: "running outcomes must be successful"
            )
        }
    }

    static func decode(_ data: Data) throws -> LifecycleDocument {
        try JSONDecoder().decode(LifecycleDocument.self, from: data)
    }
}
