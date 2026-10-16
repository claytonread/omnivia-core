import CryptoKit
import DocumentIntakeMacOS
import Foundation

struct CLICaptureDelivery: CaptureDeliveryTransport {
    let executable: URL
    let installationState: URL
    let deliveryBinding: CaptureDeliveryBinding
    /// How long one capture process may run before its answer is given up on
    /// as unknown. Production never sets it; a test shortens it to drive the
    /// real timeout path without waiting the full production deadline.
    var processDeadline: TimeInterval = CLICaptureDelivery.productionProcessDeadline

    /// The deadline production uses, and the CLI's own `--timeout-ms` stays
    /// inside it, so the CLI normally reports its own timeout before this fires.
    static let productionProcessDeadline: TimeInterval = 25

    var descriptorDigest: String {
        let fields = [
            executable.path,
            installationState.path,
            deliveryBinding.workspaceRef,
            deliveryBinding.targetRef,
            deliveryBinding.targetBindingRevision,
            deliveryBinding.callerContextBinding.clientContextRef,
            deliveryBinding.callerContextBinding.principalRef,
            deliveryBinding.callerContextBinding.contextRevision,
        ]
        let descriptor = Data(fields.joined(separator: "\u{0}").utf8)
        return "sha256:" + SHA256.hash(data: descriptor).map { String(format: "%02x", $0) }.joined()
    }

    func deliver(input: Data, payloadURL: URL, operationKey: String) async -> CaptureDeliveryOutcome {
        // The status read classifies the target and compatibility. It cannot
        // keep Core running, so the capture itself is launched with
        // `--require-running`: it attaches and can never start a service that
        // was stopped after the read. Start Core stays authoritative.
        if let refusal = Self.gate(await lifecycle(), binding: deliveryBinding) {
            return refusal
        }
        let arguments = captureArguments(payloadURL: payloadURL, operationKey: operationKey)
        switch await execute(arguments: arguments, input: input) {
        case .notLaunched: return .notSent
        case .unfinished: return .unknown
        case let .finished(status, output, diagnostic):
            return Self.outcome(status: status, output: output, diagnostic: diagnostic)
        }
    }

    /// How one CLI run ended, before anything is read into it.
    enum Run {
        /// The executable could not be launched, so it never read the input.
        case notLaunched
        /// Written to, or killed at its deadline: the input may have been received.
        case unfinished
        case finished(status: Int32, output: Data, diagnostic: Data)
    }

    /// Runs the CLI once with `input` on stdin. Both transports in this file share it, so
    /// the launch, the deadline and the "may have been received" rules are one set.
    func execute(arguments: [String], input: Data) async -> Run {
        let executable = executable
        let processDeadline = processDeadline
        return await Task.detached(priority: .utility) {
            let process = Process()
            process.executableURL = executable
            process.arguments = arguments
            let inputPipe = Pipe()
            let outputPipe = Pipe()
            let errorPipe = Pipe()
            process.standardInput = inputPipe
            process.standardOutput = outputPipe
            process.standardError = errorPipe
            // A process that cannot be launched — the executable is gone —
            // never read the input, so nothing was sent.
            do { try process.run() } catch { return .notLaunched }
            do {
                try inputPipe.fileHandleForWriting.write(contentsOf: input)
                try inputPipe.fileHandleForWriting.close()
                let deadline = Date().addingTimeInterval(processDeadline)
                while process.isRunning && Date() < deadline {
                    try? await Task.sleep(nanoseconds: 50_000_000)
                }
                guard !process.isRunning else {
                    process.terminate()
                    return .unfinished
                }
                return .finished(
                    status: process.terminationStatus,
                    output: try outputPipe.fileHandleForReading.readToEnd() ?? Data(),
                    diagnostic: try errorPipe.fileHandleForReading.readToEnd() ?? Data()
                )
            } catch {
                if process.isRunning { process.terminate() }
                return .unfinished
            }
        }.value
    }

    /// `--require-running` makes the CLI attach only. Without it the capture
    /// connects through `connect_managed_local`, which starts a stopped service.
    ///
    /// `--principal` claims the principal the intent froze, so the service
    /// compares it with the one it actually authenticates and refuses a
    /// difference (`authorization_denied`). Without a claim the service answers
    /// under its own principal, which would send an intent frozen for one
    /// account under whichever account is signed in now.
    func captureArguments(payloadURL: URL, operationKey: String) -> [String] {
        [
            "--require-running",
            "--installation-state", installationState.path,
            "--workspace-id", deliveryBinding.workspaceRef,
            "--timeout-ms", "20000",
            "document", "capture",
            "--input-stdin",
            "--payload-file", payloadURL.path,
            "--idempotency-key", operationKey,
            "--principal", deliveryBinding.callerContextBinding.principalRef,
            "--json",
        ]
    }

    /// The CLI's own redacted `service status --json`, which attaches and never
    /// starts. The companion decides nothing from the host's files or processes.
    fileprivate func lifecycle() async -> Result<LifecycleDocument, LifecycleRunnerError> {
        let runner = CLICommandRunner(
            executable: executable,
            installationState: installationState,
            workspaceID: deliveryBinding.workspaceRef,
            callbackQueue: .global(qos: .utility)
        )
        return await withCheckedContinuation { continuation in
            runner.run(.status) { continuation.resume(returning: $0) }
        }
    }

    /// `nil` lets the send proceed, and only a running service that is this
    /// frozen target does. A stopped, unreachable or unreadable one is not sent
    /// to, so the intent stays queued; an incompatible one holds it. A running
    /// service that is some other target is not a fallback.
    static func gate(
        _ result: Result<LifecycleDocument, LifecycleRunnerError>,
        binding: CaptureDeliveryBinding
    ) -> CaptureDeliveryOutcome? {
        guard case let .success(document) = result else { return .notSent }
        switch document.code {
        case .statusRunning:
            guard let target = document.safeStatus?.target,
                  target.targetRef == binding.targetRef,
                  target.workspaceRef == binding.workspaceRef
            else { return .notSent }
            return nil
        case .statusIncompatible:
            return .held(.updateRequired)
        default:
            return .notSent
        }
    }

    /// Interpret only facts Core actually returned. Error codes classify the
    /// refusal itself; they do not establish whether this operation key already
    /// committed. The runtime adds this explicit detail only when it found this
    /// exact request's committed claim but could not re-serve its answer.
    static func outcome(
        from document: [String: Any],
        terminationStatus: Int32
    ) -> CaptureDeliveryOutcome {
        if terminationStatus == 0,
           let result = document["result"] as? [String: Any],
           let captureID = result["capture_id"] as? String {
            return .admitted(captureID: captureID)
        }
        if let error = document["error"] as? [String: Any],
           let code = error["code"] as? String,
           !code.isEmpty {
            if let details = error["details"] as? [String: Any],
               details["outcome_established"] as? Bool == false {
                return .unknown
            }
            if let pause = pause(forServiceCode: code) { return .held(pause) }
            return .definiteFailure(code: code)
        }
        return .unknown
    }

    /// Service refusals that only the person can resolve. They are decided
    /// before a capture exists, so the intent is held rather than failed.
    fileprivate static func pause(forServiceCode code: String) -> CaptureQueuePause? {
        switch code {
        case "authentication_required", "authorization_denied", "workspace_not_granted",
             "capability_not_granted":
            .needsAccess
        case "incompatible_version", "unsupported_minor_version", "upgrade_required":
            .updateRequired
        default:
            nil
        }
    }

    /// The CLI's fixed diagnostics for a refusal that happens before anything is
    /// sent (`omnivia_core_cli.main`, which a test pins verbatim). They go to
    /// stderr only, so the exit status and the sentence are read together: the
    /// exit status alone is shared with failures that can follow a send.
    static let managedStartFailed = "the managed service could not be started"
    static let notRunning = "the service is not running and was not started"
    static let credentialRefused = "the service did not accept this client's credential"
    static let versionDisagreed = "this client and the service could not agree on a version"
    static let refusedBeforeSend = "the call was refused here and never sent"

    /// What one finished CLI run established. Only a service answer or one of
    /// the fixed pre-send refusals is definite; every other shape — a missing
    /// answer, an oversized one, an unlisted diagnostic — stays unknown, so an
    /// attempt that may have been received is never reported as not sent.
    static func outcome(status: Int32, output: Data, diagnostic: Data) -> CaptureDeliveryOutcome {
        if output.count <= 1024 * 1024,
           let document = try? JSONSerialization.jsonObject(with: output) as? [String: Any] {
            return outcome(from: document, terminationStatus: status)
        }
        guard output.isEmpty, diagnostic.count <= 256 else { return .unknown }
        switch (status, String(decoding: diagnostic, as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)) {
        case (1, managedStartFailed), (1, notRunning): return .notSent
        case (3, credentialRefused): return .held(.needsAccess)
        case (4, versionDisagreed): return .held(.updateRequired)
        case (2, refusedBeforeSend): return .definiteFailure(code: "refused_before_send")
        default: return .unknown
        }
    }
}

/// `document disconnect` through the same CLI, gate and process rules as `document capture`.
/// The request is the intent's frozen body and the key is the intent's one key, so a replay
/// after an unknown answer is the identical command.
extension CLICaptureDelivery: OriginalDisconnectTransport {
    func deliver(input: Data, operationKey: String) async -> OriginalDisconnectOutcome {
        if let refusal = Self.gate(await lifecycle(), binding: deliveryBinding) {
            switch refusal {
            case let .held(pause): return .held(pause)
            default: return .notSent
            }
        }
        switch await execute(arguments: disconnectArguments(operationKey: operationKey), input: input) {
        case .notLaunched: return .notSent
        case .unfinished: return .unknown
        case let .finished(status, output, diagnostic):
            return Self.disconnectOutcome(status: status, output: output, diagnostic: diagnostic)
        }
    }

    /// Same attach-only, claimed-principal rules as `captureArguments`; the request is on stdin.
    func disconnectArguments(operationKey: String) -> [String] {
        [
            "--require-running",
            "--installation-state", installationState.path,
            "--workspace-id", deliveryBinding.workspaceRef,
            "--timeout-ms", "20000",
            "document", "disconnect",
            "--input-stdin",
            "--idempotency-key", operationKey,
            "--principal", deliveryBinding.callerContextBinding.principalRef,
            "--json",
        ]
    }

    /// Only a service answer that states the binding disconnected acknowledges the intent.
    /// A refusal does not prove an earlier attempt had no effect, so it never retires anything.
    static func disconnectOutcome(status: Int32, output: Data, diagnostic: Data) -> OriginalDisconnectOutcome {
        if output.count <= 1024 * 1024,
           let document = try? JSONSerialization.jsonObject(with: output) as? [String: Any] {
            if status == 0,
               let result = document["result"] as? [String: Any],
               result["connection_state"] as? String == "disconnected",
               let documentID = result["document_id"] as? String,
               let ownerRef = result["owner_ref"] as? String {
                return .acknowledged(documentID: documentID, ownerRef: ownerRef)
            }
            if let error = document["error"] as? [String: Any],
               let code = error["code"] as? String,
               !code.isEmpty {
                if let details = error["details"] as? [String: Any],
                   details["outcome_established"] as? Bool == false {
                    return .unknown
                }
                if let pause = pause(forServiceCode: code) { return .held(pause) }
                return .definiteFailure(code: code)
            }
            return .unknown
        }
        guard output.isEmpty, diagnostic.count <= 256 else { return .unknown }
        switch (status, String(decoding: diagnostic, as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)) {
        case (1, managedStartFailed), (1, notRunning): return .notSent
        case (3, credentialRefused): return .held(.needsAccess)
        case (4, versionDisagreed): return .held(.updateRequired)
        case (2, refusedBeforeSend): return .definiteFailure(code: "refused_before_send")
        default: return .unknown
        }
    }
}
