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
            process.standardInput = inputPipe
            process.standardOutput = outputPipe
            process.standardError = FileHandle.nullDevice
            do {
                try process.run()
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
                let output = try outputPipe.fileHandleForReading.readToEnd() ?? Data()
                guard output.count <= 1024 * 1024,
                      let document = try JSONSerialization.jsonObject(with: output) as? [String: Any]
                else { return .unknown }
                return Self.outcome(
                    from: document,
                    terminationStatus: process.terminationStatus
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
}
