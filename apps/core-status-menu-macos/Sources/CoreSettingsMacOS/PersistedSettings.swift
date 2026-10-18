// ============================================================================
// Workspace-scoped persisted settings, as the Application Contract publishes
// them.
//
// Two operations, `settings.list` and `settings.set`, reached the same way the
// Access pane reaches `mcp status|configure|revoke`: the window runs the
// installed `omnivia` CLI and reads what comes back. It owns no store, holds no
// lease and opens no database — the service that owns the workspace does the
// writing, inside its own fence.
//
// Three things about that CLI shape the code below:
//
// * Without `--json` an application command prints the operation's *result*
//   document on success and nothing on stdout on a refusal, so stdout is
//   decoded directly rather than unwrapped from a response envelope.
// * `settings set` is a mutation whose catalogue entry requires an idempotency
//   key, so one is always sent. A retry of the same change must carry the same
//   key to be a replay rather than a second write, which is why the key is a
//   parameter and not generated in here.
// * Both commands need `--workspace-id`: a setting belongs to one workspace.
//   That is the difference from `McpCommandRunner`, which administers the
//   installation and takes no workspace.
//
// A value is carried as a JSON-encoded string in both directions, which is the
// contract's own `SettingValueJson`. The service canonicalises it — sorted
// keys, no insignificant whitespace — so a value read back is semantically but
// not textually what was written.
// ============================================================================

import Foundation

/// One stored setting as the contract's `SettingRecord`.
public struct StoredSetting: Sendable, Equatable {
    public let key: String
    public let valueJSON: String
    /// Opaque, and treated as opaque here: it is shown and compared, never parsed.
    public let recordVersion: String
    public let updatedAt: String
    public let updatedBy: String

    public init(
        key: String, valueJSON: String, recordVersion: String,
        updatedAt: String, updatedBy: String
    ) {
        self.key = key
        self.valueJSON = valueJSON
        self.recordVersion = recordVersion
        self.updatedAt = updatedAt
        self.updatedBy = updatedBy
    }
}

public enum SettingsError: Error, Sendable, Equatable {
    case unavailable          // no CLI to run
    case timedOut
    case unreadableOutput
    case refused              // the service answered with an error
}

public protocol PersistedSettings: Sendable {
    /// Every stored setting, or one namespace's. A `prefix` names the namespace
    /// without a trailing dot -- the contract's key grammar admits none -- and the
    /// service matches it on the namespace boundary.
    func list(prefix: String?) async -> Result<[StoredSetting], SettingsError>
    /// Store `valueJSON` under `key`, returning the setting as it now stands.
    func set(
        key: String, valueJSON: String, idempotencyKey: String
    ) async -> Result<StoredSetting, SettingsError>
}

/// Runs the installed `omnivia` CLI against one workspace.
public struct SettingsCommandRunner: PersistedSettings {
    private let executable: URL
    private let installationState: URL
    private let workspaceID: String
    private let queue = DispatchQueue(
        label: "com.omnivia.core-settings.settings", qos: .userInitiated
    )

    public init(executable: URL, installationState: URL, workspaceID: String) {
        self.executable = executable
        self.installationState = installationState
        self.workspaceID = workspaceID
    }

    // A settings call is one round trip to a local service: no child process of
    // its own, nothing to count, no file to walk.
    private static let timeout: TimeInterval = 10

    public func list(prefix: String? = nil) async -> Result<[StoredSetting], SettingsError> {
        let input = prefix.map { Self.listInput(prefix: $0) } ?? "{}"
        switch await invoke(["settings", "list", "--input-json", input]) {
        case .failure(let error): return .failure(error)
        case .success(let output):
            guard output.code == 0 else { return .failure(.refused) }
            guard let records = Self.decodeList(output.stdout) else {
                return .failure(.unreadableOutput)
            }
            return .success(records)
        }
    }

    public func set(
        key: String, valueJSON: String, idempotencyKey: String
    ) async -> Result<StoredSetting, SettingsError> {
        let arguments = [
            "settings", "set",
            "--input-json", Self.setInput(key: key, valueJSON: valueJSON),
            "--idempotency-key", idempotencyKey,
        ]
        switch await invoke(arguments) {
        case .failure(let error): return .failure(error)
        case .success(let output):
            guard output.code == 0 else { return .failure(.refused) }
            guard let record = Self.decodeSet(output.stdout) else {
                return .failure(.unreadableOutput)
            }
            return .success(record)
        }
    }

    // MARK: Input documents

    /// Built with `JSONSerialization` rather than string interpolation: a key or
    /// a value carrying a quote or a backslash would otherwise produce a
    /// document the CLI refuses, and the value is arbitrary JSON text.
    static func encode(_ object: [String: String]) -> String {
        guard let data = try? JSONSerialization.data(
                withJSONObject: object, options: [.sortedKeys]),
              let text = String(data: data, encoding: .utf8)
        else { return "{}" }
        return text
    }

    static func listInput(prefix: String) -> String { encode(["prefix": prefix]) }

    static func setInput(key: String, valueJSON: String) -> String {
        encode(["key": key, "value_json": valueJSON])
    }

    // MARK: Results

    /// `{"settings":[…]}`. A row missing a field makes the whole answer
    /// unreadable rather than silently shorter: a settings list the window
    /// renders as complete must be complete.
    static func decodeList(_ text: String) -> [StoredSetting]? {
        guard let data = text.data(using: .utf8),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let rows = root["settings"] as? [[String: Any]]
        else { return nil }
        var records: [StoredSetting] = []
        for row in rows {
            guard let record = record(from: row) else { return nil }
            records.append(record)
        }
        return records
    }

    /// `{"setting":{…}}`.
    static func decodeSet(_ text: String) -> StoredSetting? {
        guard let data = text.data(using: .utf8),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let row = root["setting"] as? [String: Any]
        else { return nil }
        return record(from: row)
    }

    static func record(from row: [String: Any]) -> StoredSetting? {
        guard let key = row["key"] as? String,
              let valueJSON = row["value_json"] as? String,
              let recordVersion = row["record_version"] as? String,
              let updatedAt = row["updated_at"] as? String,
              let updatedBy = row["updated_by"] as? String
        else { return nil }
        return StoredSetting(
            key: key, valueJSON: valueJSON, recordVersion: recordVersion,
            updatedAt: updatedAt, updatedBy: updatedBy
        )
    }

    // MARK: Process

    private struct Output { let stdout: String; let code: Int32 }

    private func invoke(_ arguments: [String]) async -> Result<Output, SettingsError> {
        await withCheckedContinuation { continuation in
            queue.async {
                let process = Process()
                process.executableURL = executable
                process.arguments =
                    [
                        "--installation-state", installationState.path,
                        "--workspace-id", workspaceID,
                    ] + arguments
                let out = Pipe()
                process.standardOutput = out
                process.standardError = Pipe()   // fixed sentences; not shown

                do {
                    try process.run()
                } catch {
                    continuation.resume(returning: .failure(.unavailable))
                    return
                }

                // Drain before waiting: a full pipe buffer would deadlock the
                // child against our own wait.
                var data = Data()
                let reader = DispatchQueue(
                    label: "com.omnivia.core-settings.settings.read"
                )
                let drained = DispatchSemaphore(value: 0)
                reader.async {
                    data = (try? out.fileHandleForReading.readToEnd()) ?? Data()
                    drained.signal()
                }

                let finished = DispatchSemaphore(value: 0)
                DispatchQueue.global().async { process.waitUntilExit(); finished.signal() }
                if finished.wait(timeout: .now() + Self.timeout) == .timedOut {
                    process.terminate()
                    _ = drained.wait(timeout: .now() + 2)
                    continuation.resume(returning: .failure(.timedOut))
                    return
                }
                _ = drained.wait(timeout: .now() + 2)
                continuation.resume(returning: .success(
                    Output(
                        stdout: String(decoding: data, as: UTF8.self),
                        code: process.terminationStatus
                    )
                ))
            }
        }
    }
}
