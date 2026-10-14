// ============================================================================
// Installed-MCP administration, as the Access pane needs it.
//
// The pane's "connected applications" are the installation's MCP hosts. This
// wraps `omnivia … mcp status|configure|revoke`, the same thin-client pattern
// the menu uses for `service start|stop|status` — the window runs the
// installed CLI and renders what comes back. It owns no store and no lease.
//
// Three things about that CLI shape the code below:
//
// * `mcp status --json` exits 1 when the service is unreachable but still
//   prints a valid document, with the on-disk half filled in. A non-zero exit
//   is therefore not a failure here, unlike the lifecycle adapter.
// * `configure` and `revoke` print no JSON — a host-native snippet and
//   `revoked <host>` respectively — so they are read by exit code.
// * both of them START a service if none is running. §A2 forbids the settings
//   window registering or starting a background component as a side effect,
//   so the pane only offers them when status already reports the service
//   reachable. That is a UI gate, stated here because it is the reason this
//   type exposes `serviceReachable`.
// ============================================================================

import Foundation

/// One host's row of `mcp status --json`, decoded strictly.
public struct McpHostStatus: Sendable, Equatable {
    public enum Presence: String, Sendable {
        case absent, present, unusable, mismatched
    }
    public enum Grant: String, Sendable {
        case unknown, absent, active, revoked
    }
    public enum Profile: String, Sendable {
        case restricted, authoring

        /// The design's wording for the access level. `authoring` carries the
        /// `workspace_contributor` role and 11 tools against `restricted`'s 6.
        public var accessLevel: String {
            self == .authoring ? "Read and contribute" : "Read only"
        }
    }

    public let host: String
    public let configuration: Presence
    public let credential: Presence
    public let grant: Grant
    public let profile: Profile?
    public let principalID: String?
    public let workspaceID: String?
    public let serviceReachable: Bool
    public let advertisedToolCount: Int?
    public let authoringIntent: Bool?

    public init(
        host: String, configuration: Presence, credential: Presence, grant: Grant,
        profile: Profile?, principalID: String?, workspaceID: String?,
        serviceReachable: Bool, advertisedToolCount: Int?, authoringIntent: Bool?
    ) {
        self.host = host
        self.configuration = configuration
        self.credential = credential
        self.grant = grant
        self.profile = profile
        self.principalID = principalID
        self.workspaceID = workspaceID
        self.serviceReachable = serviceReachable
        self.advertisedToolCount = advertisedToolCount
        self.authoringIntent = authoringIntent
    }

    /// There is no display name in the model — only the host enum — so the
    /// name shown is derived here rather than invented per call site.
    public var displayName: String {
        switch host {
        case "claude-code": return "Claude Code"
        case "codex": return "Codex"
        default: return host
        }
    }
}

public struct McpStatus: Sendable, Equatable {
    public let version: Int
    public let hosts: [McpHostStatus]

    public init(version: Int, hosts: [McpHostStatus]) {
        self.version = version
        self.hosts = hosts
    }

    /// Reachability is reported per host but is a property of the one service.
    public var serviceReachable: Bool { hosts.contains { $0.serviceReachable } }
}

public enum McpError: Error, Sendable, Equatable {
    case unavailable          // no CLI to run
    case timedOut
    case unreadableOutput
    case notAdministrator     // exit 3
    case failed               // exit 1 with no usable document
}

public protocol McpAdministering: Sendable {
    func status() async -> Result<McpStatus, McpError>
    func configure(host: String, workspace: String, profile: String) async -> Result<Void, McpError>
    func revoke(host: String) async -> Result<Void, McpError>
}

/// Runs the installed `omnivia` CLI.
public struct McpCommandRunner: McpAdministering {
    private let executable: URL
    private let installationState: URL
    private let queue = DispatchQueue(label: "com.omnivia.core-settings.mcp", qos: .userInitiated)

    public init(executable: URL, installationState: URL) {
        self.executable = executable
        self.installationState = installationState
    }

    // `mcp status` may start a child MCP server per configured host to count
    // advertised tools, so it is not as quick as a lifecycle probe.
    private static let statusTimeout: TimeInterval = 20
    private static let mutateTimeout: TimeInterval = 60

    public func status() async -> Result<McpStatus, McpError> {
        let run = await invoke(["mcp", "status", "--json"], timeout: Self.statusTimeout)
        switch run {
        case .failure(let error): return .failure(error)
        case .success(let output):
            // Exit status is deliberately ignored: 1 means "service
            // unreachable", which is a state the pane shows, not an error.
            guard let decoded = Self.decode(output.stdout) else {
                return .failure(output.code == 3 ? .notAdministrator : .unreadableOutput)
            }
            return .success(decoded)
        }
    }

    public func configure(
        host: String, workspace: String, profile: String
    ) async -> Result<Void, McpError> {
        await mutate([
            "mcp", "configure", "--host", host, "--workspace", workspace, "--profile", profile,
        ])
    }

    public func revoke(host: String) async -> Result<Void, McpError> {
        await mutate(["mcp", "revoke", "--host", host])
    }

    private func mutate(_ arguments: [String]) async -> Result<Void, McpError> {
        switch await invoke(arguments, timeout: Self.mutateTimeout) {
        case .failure(let error): return .failure(error)
        case .success(let output):
            switch output.code {
            case 0: return .success(())
            case 3: return .failure(.notAdministrator)
            default: return .failure(.failed)
            }
        }
    }

    private struct Output { let stdout: String; let code: Int32 }

    private func invoke(
        _ arguments: [String], timeout: TimeInterval
    ) async -> Result<Output, McpError> {
        await withCheckedContinuation { continuation in
            queue.async {
                let process = Process()
                process.executableURL = executable
                process.arguments =
                    ["--installation-state", installationState.path] + arguments
                let out = Pipe()
                process.standardOutput = out
                process.standardError = Pipe()   // diagnostics are fixed sentences; not shown

                do {
                    try process.run()
                } catch {
                    continuation.resume(returning: .failure(.unavailable))
                    return
                }

                // Drain before waiting: a full pipe buffer would deadlock the
                // child against our own wait.
                var data = Data()
                let reader = DispatchQueue(label: "com.omnivia.core-settings.mcp.read")
                let drained = DispatchSemaphore(value: 0)
                reader.async {
                    data = (try? out.fileHandleForReading.readToEnd()) ?? Data()
                    drained.signal()
                }

                let finished = DispatchSemaphore(value: 0)
                DispatchQueue.global().async { process.waitUntilExit(); finished.signal() }
                if finished.wait(timeout: .now() + timeout) == .timedOut {
                    process.terminate()
                    _ = drained.wait(timeout: .now() + 2)
                    continuation.resume(returning: .failure(.timedOut))
                    return
                }
                _ = drained.wait(timeout: .now() + 2)
                continuation.resume(returning: .success(
                    Output(stdout: String(decoding: data, as: UTF8.self), code: process.terminationStatus)
                ))
            }
        }
    }

    /// Decode `{"mcp_status_version":1,"hosts":[…]}`. Unknown enum values make
    /// the row unreadable rather than silently becoming a default — a status
    /// this build does not understand must not render as a healthy one.
    static func decode(_ text: String) -> McpStatus? {
        guard let data = text.data(using: .utf8),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let version = root["mcp_status_version"] as? Int,
              version == 1,
              let rows = root["hosts"] as? [[String: Any]]
        else { return nil }

        var hosts: [McpHostStatus] = []
        for row in rows {
            guard let host = row["host"] as? String,
                  let configuration = (row["configuration"] as? String)
                    .flatMap(McpHostStatus.Presence.init(rawValue:)),
                  let credential = (row["credential"] as? String)
                    .flatMap(McpHostStatus.Presence.init(rawValue:)),
                  let grant = (row["grant"] as? String)
                    .flatMap(McpHostStatus.Grant.init(rawValue:)),
                  let service = row["service"] as? String,
                  service == "reachable" || service == "unreachable"
            else { return nil }
            hosts.append(McpHostStatus(
                host: host,
                configuration: configuration,
                credential: credential,
                grant: grant,
                profile: (row["profile"] as? String).flatMap(McpHostStatus.Profile.init(rawValue:)),
                principalID: row["principal_id"] as? String,
                workspaceID: row["workspace_id"] as? String,
                serviceReachable: service == "reachable",
                advertisedToolCount: row["advertised_tool_count"] as? Int,
                authoringIntent: row["authoring_intent"] as? Bool
            ))
        }
        return McpStatus(version: version, hosts: hosts)
    }
}
