import CryptoKit
import DocumentIntakeMacOS
import Foundation

struct CLICaptureDelivery: CaptureDeliveryTransport {
    let executable: URL
    let installationState: URL
    let deliveryBinding: CaptureDeliveryBinding

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
        await Task.detached(priority: .utility) {
            let process = Process()
            process.executableURL = executable
            process.arguments = [
                "--installation-state", installationState.path,
                "--workspace-id", deliveryBinding.workspaceRef,
                "--timeout-ms", "20000",
                "document", "capture",
                "--input-stdin",
                "--payload-file", payloadURL.path,
                "--idempotency-key", operationKey,
                "--json",
            ]
            let inputPipe = Pipe()
            let outputPipe = Pipe()
            let errorPipe = Pipe()
            process.standardInput = inputPipe
            process.standardOutput = outputPipe
            process.standardError = errorPipe
            // A process that cannot be launched — the executable is gone —
            // never read the input, so nothing was sent.
            do { try process.run() } catch { return .notSent }
            do {
                try inputPipe.fileHandleForWriting.write(contentsOf: input)
                try inputPipe.fileHandleForWriting.close()
                let deadline = Date().addingTimeInterval(25)
                while process.isRunning && Date() < deadline {
                    try? await Task.sleep(nanoseconds: 50_000_000)
                }
                guard !process.isRunning else {
                    process.terminate()
                    return .unknown
                }
                return Self.outcome(
                    status: process.terminationStatus,
                    output: try outputPipe.fileHandleForReading.readToEnd() ?? Data(),
                    diagnostic: try errorPipe.fileHandleForReading.readToEnd() ?? Data()
                )
            } catch {
                if process.isRunning { process.terminate() }
                return .unknown
            }
        }.value
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
            return .definiteFailure(code: code)
        }
        return .unknown
    }

    /// The CLI's fixed diagnostics for a refusal that happens before anything is
    /// sent (`omnivia_core_cli.main`, which a test pins verbatim). They go to
    /// stderr only, so the exit status and the sentence are read together: the
    /// exit status alone is shared with failures that can follow a send.
    static let managedStartFailed = "the managed service could not be started"
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
        case (1, managedStartFailed): return .notSent
        case (3, credentialRefused): return .definiteFailure(code: "target_credential_refused")
        case (4, versionDisagreed): return .definiteFailure(code: "target_incompatible")
        case (2, refusedBeforeSend): return .definiteFailure(code: "refused_before_send")
        default: return .unknown
        }
    }
}
