// ============================================================================
// The update check, as General > Updates needs it.
//
// The authority is the installed client's read-only
// `omnivia … service update-check --json`. It fetches the first-party channel,
// compares versions and answers with one of six statuses. It installs nothing,
// downloads no release asset and starts nothing, so a person pressing Check Now
// can only ever learn something. This file is the thin client for it and the
// model the pane renders; it owns no store, no schedule and no installer.
//
// What the CLI does that shapes the code below:
//
// * `check_failed` and `unsupported_install` exit 1 but still print the whole
//   adapter document. A non-zero exit with a valid document is therefore an
//   answer, not a failure, and the document alone decides.
// * `--json` is a leaf option, so it follows `update-check`. The root parser
//   also requires `--installation-state` and, for a `service` command, the
//   workspace the target is resolved for.
// * A document is accepted only if it is internally consistent. A status this
//   build does not know, a member of the wrong type, an unexpected member, or
//   fields that contradict the status (a candidate on an `up_to_date`, a
//   missing reason on a `check_failed`) is unreadable, never a best guess.
// ============================================================================

import CoreFoundation
import Foundation

/// The six statuses of adapter version 1.
public enum CoreUpdateStatus: String, Sendable, Equatable, CaseIterable {
    case upToDate = "up_to_date"
    case updateAvailable = "update_available"
    case aheadOfChannel = "ahead_of_channel"
    case noRelease = "no_release"
    case checkFailed = "check_failed"
    case unsupportedInstall = "unsupported_install"

    /// The two statuses the CLI answers with exit 1 and `ok: false`.
    public var isFailure: Bool { self == .checkFailed || self == .unsupportedInstall }
}

/// One validated adapter document.
public struct CoreUpdateCheckResult: Sendable, Equatable {
    public let status: CoreUpdateStatus
    public let checkedAt: Date
    /// Present exactly when `status` is `updateAvailable`.
    public let candidateVersion: String?
    /// Present exactly when `status` is a failure.
    public let reason: String?

    public init(
        status: CoreUpdateStatus, checkedAt: Date,
        candidateVersion: String? = nil, reason: String? = nil
    ) {
        self.status = status
        self.checkedAt = checkedAt
        self.candidateVersion = candidateVersion
        self.reason = reason
    }

    /// The reason as the pane shows it: control characters and runs of space
    /// folded to one space, and cut to a sentence-sized length. The CLI's
    /// reason is prose built from an exception message, so it is bounded here
    /// rather than trusted to be short.
    public var safeReason: String? {
        guard let reason else { return nil }
        var folded = ""
        var pendingSpace = false
        for scalar in reason.unicodeScalars {
            if scalar.properties.isWhitespace || scalar.properties.generalCategory == .control {
                pendingSpace = !folded.isEmpty
            } else {
                if pendingSpace { folded.append(" ") }
                pendingSpace = false
                folded.unicodeScalars.append(scalar)
            }
        }
        return folded.count > Self.reasonDisplayLimit
            ? String(folded.prefix(Self.reasonDisplayLimit)) + "…"
            : folded
    }

    static let reasonDisplayLimit = 200
}

/// Why no result is shown. Each sentence names the state, and every one is true
/// of a check that changed nothing.
public enum CoreUpdateError: Error, Sendable, Equatable {
    /// No installed `omnivia` to run, or it could not be started.
    case unavailable
    case timedOut
    /// Output that is not a valid, consistent adapter document.
    case unreadableOutput
    /// The output outran its bound, so none of it was read.
    case outputTooLarge
    /// The client ran, exited non-zero and printed no adapter document. Its
    /// local refusals (usage, credential, target) are not update answers.
    case noAnswer(exitCode: Int32)

    public var sentence: String {
        switch self {
        case .unavailable:
            return "This window has no route to the installed Core client, so it cannot "
                + "check for updates."
        case .timedOut:
            return "The update check did not finish in time. Nothing was changed."
        case .unreadableOutput:
            return "The installed Core client answered with something this build cannot "
                + "read, so no result is shown. Nothing was changed."
        case .outputTooLarge:
            return "The installed Core client printed more than an update check should, so "
                + "none of it was read. Nothing was changed."
        case .noAnswer(let exitCode):
            return "The installed Core client exited with status \(exitCode) without an "
                + "update result, so nothing is known about updates. Nothing was changed."
        }
    }
}

public protocol CoreUpdateAdministering: Sendable {
    /// One read-only check. Never installs, downloads or changes anything.
    func check() async -> Result<CoreUpdateCheckResult, CoreUpdateError>
}

/// Runs the installed `omnivia` once for `service update-check`.
///
/// The process seam is the module's existing bounded one: an argument vector,
/// no shell, a null stdin, both streams drained to the end while the child runs
/// and a bounded wait.
public struct CoreUpdateCommandRunner: CoreUpdateAdministering {
    private let executable: URL
    private let installationState: URL
    private let workspaceRef: String
    private let process: any OcrProcessRunning
    private let timeout: TimeInterval

    /// Nil unless both paths are absolute and a workspace is named, because the
    /// CLI refuses a relative installation state and resolves a target for the
    /// workspace before it runs a `service` command.
    public init?(
        executable: URL,
        installationState: URL,
        workspaceRef: String,
        timeout: TimeInterval = CoreUpdateCommandRunner.defaultTimeout,
        process: any OcrProcessRunning = SystemOcrProcessRunner()
    ) {
        guard executable.isFileURL, executable.path.hasPrefix("/"),
              installationState.isFileURL, installationState.path.hasPrefix("/"),
              !workspaceRef.isEmpty
        else { return nil }
        self.executable = executable.standardizedFileURL
        self.installationState = installationState.standardizedFileURL
        self.workspaceRef = workspaceRef
        self.timeout = timeout
        self.process = process
    }

    /// The channel fetch alone is bounded at ten seconds inside the client, and
    /// the interpreter has to start first.
    public static let defaultTimeout: TimeInterval = 30
    /// A result is a few hundred bytes. This is far past any honest one, and the
    /// diagnostic is a line.
    static let stdoutLimit = 64 << 10
    static let stderrLimit = 64 << 10

    /// The exact argument vector, built here and nowhere else.
    static func arguments(installationState: URL, workspaceRef: String) -> [String] {
        [
            "--installation-state", installationState.path,
            "--workspace-id", workspaceRef,
            "service", "update-check", "--json",
        ]
    }

    public func check() async -> Result<CoreUpdateCheckResult, CoreUpdateError> {
        let run = await process.run(
            executable: executable,
            arguments: Self.arguments(
                installationState: installationState, workspaceRef: workspaceRef
            ),
            timeout: timeout,
            stdoutLimit: Self.stdoutLimit,
            stderrLimit: Self.stderrLimit
        )
        switch run {
        case .failure(.spawnFailed): return .failure(.unavailable)
        case .failure(.timedOut): return .failure(.timedOut)
        case .success(let output):
            guard !output.truncated else { return .failure(.outputTooLarge) }
            // The exit status is deliberately not what decides: the two failure
            // statuses exit 1 and still print the document. A non-zero status
            // with *no* document is the other thing, a refusal before the check.
            let answer = Self.decode(output.stdout)
            switch answer {
            case .failure(.unreadableOutput) where output.code != 0:
                return .failure(.noAnswer(exitCode: output.code))
            case .success(let result) where result.status.isFailure == (output.code == 0):
                // The status and the exit code are two statements of one fact
                // (`ok`). A document that disagrees with its own exit is
                // unreadable, not a best guess.
                return .failure(.unreadableOutput)
            default:
                return answer
            }
        }
    }

    // MARK: Decoding

    private static let members: Set<String> = [
        "update_check_adapter_version", "product_id", "status", "installed",
        "checked_at", "channel_url", "ok",
        "reason", "candidate_version", "release_url", "bundle_sha256",
    ]
    static let productID = "omnivia-core"
    static let adapterVersion = 1
    private static let reasonLimit = 4096

    /// Read `text` as one adapter document, or refuse all of it.
    static func decode(_ text: String) -> Result<CoreUpdateCheckResult, CoreUpdateError> {
        guard let result = parse(text) else { return .failure(.unreadableOutput) }
        return .success(result)
    }

    private static func parse(_ text: String) -> CoreUpdateCheckResult? {
        guard let data = text.data(using: .utf8),
              let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              Set(root.keys).isSubset(of: members)
        else { return nil }

        // JSONSerialization hands booleans back as NSNumber, which Swift will
        // happily read as 0 or 1. A flag is only a flag if it was written as one.
        func isBoolean(_ value: Any) -> Bool {
            CFGetTypeID(value as CFTypeRef) == CFBooleanGetTypeID()
        }
        func string(_ key: String) -> String?? {
            guard let value = root[key] else { return .some(nil) }      // absent
            guard let text = value as? String else { return nil }       // wrong type or null
            return .some(text)
        }

        guard let versionValue = root["update_check_adapter_version"],
              !isBoolean(versionValue), versionValue as? Int == adapterVersion,
              let okValue = root["ok"], isBoolean(okValue), let ok = okValue as? Bool,
              case .some(.some(let product)) = string("product_id"), product == productID,
              case .some(.some(let statusText)) = string("status"),
              let status = CoreUpdateStatus(rawValue: statusText),
              case .some(.some(let checkedText)) = string("checked_at"),
              checkedText.hasSuffix("Z"),
              let checkedAt = ISO8601DateFormatter().date(from: checkedText),
              case .some(.some) = string("channel_url"),
              let installed = root["installed"] as? [String: Any],
              installed.count <= 32, installed.values.allSatisfy({ $0 is String }),
              case .some(let reason) = string("reason"),
              case .some(let candidate) = string("candidate_version"),
              case .some(let releaseURL) = string("release_url"),
              case .some(let bundle) = string("bundle_sha256")
        else { return nil }

        // `ok` restates the status and must agree with it.
        guard ok == !status.isFailure else { return nil }

        if status.isFailure {
            guard let reason, !reason.isEmpty, reason.count <= reasonLimit,
                  candidate == nil, releaseURL == nil, bundle == nil
            else { return nil }
            return CoreUpdateCheckResult(status: status, checkedAt: checkedAt, reason: reason)
        }
        guard reason == nil else { return nil }
        guard status == .updateAvailable else {
            guard candidate == nil, releaseURL == nil, bundle == nil else { return nil }
            return CoreUpdateCheckResult(status: status, checkedAt: checkedAt)
        }
        guard let candidate, isDottedInteger(candidate),
              let releaseURL, releaseURL.hasPrefix("https://"), releaseURL.count <= 2048
        else { return nil }
        if let bundle {
            // `sha256:` and 64 lowercase hex digits, the channel's own rule.
            guard bundle.hasPrefix("sha256:"), bundle.count == 71,
                  bundle.dropFirst(7).allSatisfy({ "0123456789abcdef".contains($0) })
            else { return nil }
        }
        return CoreUpdateCheckResult(
            status: status, checkedAt: checkedAt, candidateVersion: candidate
        )
    }

    /// Core's dotted-integer version form: `0` or digits without a leading zero,
    /// joined by dots.
    private static func isDottedInteger(_ value: String) -> Bool {
        guard !value.isEmpty, value.count <= 64 else { return false }
        return value.split(separator: ".", omittingEmptySubsequences: false).allSatisfy { part in
            !part.isEmpty && part.allSatisfy { $0.isASCII && $0.isNumber }
                && (part == "0" || !part.hasPrefix("0"))
        }
    }
}

// MARK: - The pane's model

/// What General > Updates knows about the last check.
///
/// The window renders this and calls into it. Three decisions live here so they
/// can be tested without AppKit:
///
/// * Nothing runs until `checkNow` is called. There is no check on open, no
///   timer and no preference, because no durable owner of an automatic-check
///   choice exists.
/// * A check in flight refuses a second one, so a double click or a click on a
///   stale render cannot start two children.
/// * The last outcome replaces the one before it, success or failure, so the pane
///   never shows an old answer beside a newer failure.
@MainActor
public final class CoreUpdateCheckModel {
    public enum State: Equatable {
        case neverChecked
        case checking
        case result(CoreUpdateCheckResult)
        case failed(CoreUpdateError)
    }

    public private(set) var state: State

    /// Called after every state change, on the main actor.
    public var onChange: (() -> Void)?

    private let administering: CoreUpdateAdministering?

    /// Nil means no route to a client. The pane then shows that and offers no
    /// check, rather than inviting one that can only fail.
    public init(administering: CoreUpdateAdministering?) {
        self.administering = administering
        state = administering == nil ? .failed(.unavailable) : .neverChecked
    }

    public var canCheck: Bool { administering != nil && state != .checking }

    /// Returns whether a check ran: false for no route or one already running.
    @discardableResult
    public func checkNow() async -> Bool {
        guard let administering, canCheck else { return false }
        state = .checking
        onChange?()
        switch await administering.check() {
        case .success(let result): state = .result(result)
        case .failure(let error): state = .failed(error)
        }
        onChange?()
        return true
    }
}
