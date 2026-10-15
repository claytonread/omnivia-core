import Foundation

/// The authenticated application-operation transport this companion already uses, applied
/// to the two document operations the management window needs.
///
/// It runs the installed `omnivia` client over the governed application surface, the same
/// way `document capture` is delivered: the operation is named by its argv path, its input
/// is one bounded JSON object on standard input, and the answer is the canonical response
/// envelope. There is no second path here -- no SQLite file is opened, no maintenance
/// command is invoked, no workspace is overridden and no principal is claimed. The
/// workspace is the one global flag the companion was configured with, and purpose, scope
/// and capability are the client's own table lookups keyed by operation.
///
/// `document history` is a read and carries no idempotency key: the CLI refuses a key an
/// operation does not accept, and a usage refusal would read as a transient failure.
/// `document reprocess` requires one, and the key it gets is the exact-replay key derived
/// in `CoreDocumentHistoryReader.reprocessOperationKey`.
public struct CLICoreDocumentHistoryTransport: CoreDocumentHistoryTransport {
    /// The largest answer this surface will read. A page is bounded by the operation, so
    /// anything past this is not an answer worth decoding.
    static let maximumOutputBytes = 4 * 1024 * 1024
    static let historyTimeoutSeconds = 20.0
    static let reprocessTimeoutSeconds = 180.0

    private let executable: URL
    private let installationState: URL
    private let workspaceRef: String

    public init(executable: URL, installationState: URL, workspaceRef: String) {
        self.executable = executable
        self.installationState = installationState
        self.workspaceRef = workspaceRef
    }

    public func history(
        documentID: String,
        limit: Int,
        continuationToken: String?
    ) -> Result<CoreDocumentHistoryPage, CoreHistoryError> {
        var input: [String: Any] = ["document_id": documentID, "limit": limit]
        if let continuationToken {
            input["page"] = ["continuation_token": continuationToken]
        }
        return invoke(
            leaf: "history",
            input: input,
            idempotencyKey: nil,
            timeout: Self.historyTimeoutSeconds,
            timeoutMilliseconds: 20_000
        )
    }

    public func reprocess(
        captureID: String,
        profileRef: String,
        revision: String,
        expectedProfileDigest: String,
        expectedBaseExtractionID: String?,
        operationKey: String
    ) -> Result<CoreReprocessReceipt, CoreHistoryError> {
        // `expected_base` is always sent. An absent `extraction_id` inside it is the
        // caller's explicit statement that it observed no successful extraction, which is
        // a different claim from omitting the precondition.
        var base: [String: Any] = [:]
        if let expectedBaseExtractionID { base["extraction_id"] = expectedBaseExtractionID }
        // The expected profile digest is sent verbatim from the history read. It is the
        // precondition that stops a newer profile, or newer OCR resources, being run
        // beneath a request that named the one the user was shown: Core recomputes the
        // identity and refuses rather than substituting.
        let input: [String: Any] = [
            "capture_id": captureID,
            "processing_profile": [
                "profile_ref": profileRef,
                "revision": revision,
                "expected_profile_digest": expectedProfileDigest,
            ],
            "expected_base": base,
        ]
        return invoke(
            leaf: "reprocess",
            input: input,
            idempotencyKey: operationKey,
            timeout: Self.reprocessTimeoutSeconds,
            timeoutMilliseconds: 170_000
        )
    }

    private func invoke<Value: Decodable>(
        leaf: String,
        input: [String: Any],
        idempotencyKey: String?,
        timeout: TimeInterval,
        timeoutMilliseconds: Int
    ) -> Result<Value, CoreHistoryError> {
        guard let document = try? JSONSerialization.data(withJSONObject: input, options: []) else {
            return .failure(.unreadable)
        }
        var arguments = [
            "--installation-state", installationState.path,
            "--workspace-id", workspaceRef,
            "--timeout-ms", String(timeoutMilliseconds),
            "document", leaf,
            "--input-stdin",
            "--json",
        ]
        if let idempotencyKey {
            arguments += ["--idempotency-key", idempotencyKey]
        }
        let outcome = Self.run(
            executable: executable,
            arguments: arguments,
            input: document,
            timeout: timeout
        )
        switch outcome {
        case let .failure(error):
            return .failure(error)
        case let .success(output):
            return Self.decode(output)
        }
    }

    /// One bounded invocation, with both pipes drained concurrently so a chatty adapter
    /// cannot deadlock on a full pipe, and stderr read and discarded: a human diagnostic
    /// is not this surface's to read, let alone to show.
    static func run(
        executable: URL,
        arguments: [String],
        input: Data,
        timeout: TimeInterval
    ) -> Result<Data, CoreHistoryError> {
        let process = Process()
        let standardInput = Pipe()
        let standardOutput = Pipe()
        let standardError = Pipe()
        let terminated = DispatchSemaphore(value: 0)
        process.executableURL = executable
        process.arguments = arguments
        process.standardInput = standardInput
        process.standardOutput = standardOutput
        process.standardError = standardError
        process.terminationHandler = { _ in terminated.signal() }
        do {
            try process.run()
        } catch {
            return .failure(.unavailable)
        }
        let collected = CoreHistoryLockedData()
        let readers = DispatchGroup()
        readers.enter()
        DispatchQueue.global(qos: .utility).async {
            collected.replace(with: (try? standardOutput.fileHandleForReading.readToEnd()) ?? Data())
            readers.leave()
        }
        readers.enter()
        DispatchQueue.global(qos: .utility).async {
            _ = try? standardError.fileHandleForReading.readToEnd()
            readers.leave()
        }
        try? standardInput.fileHandleForWriting.write(contentsOf: input)
        try? standardInput.fileHandleForWriting.close()
        if terminated.wait(timeout: .now() + timeout) == .timedOut {
            process.terminate()
            _ = terminated.wait(timeout: .now() + 2)
            _ = readers.wait(timeout: .now() + 2)
            return .failure(.timedOut)
        }
        _ = readers.wait(timeout: .now() + 2)
        let output = collected.value()
        guard output.count <= maximumOutputBytes else { return .failure(.unreadable) }
        return .success(output)
    }

    /// The canonical response envelope, read strictly.
    ///
    /// The envelope is split before anything is decoded, because its two branches are
    /// different answers rather than two shapes of one. An error branch is decoded to its
    /// frozen code so the window can say which state it is in instead of collapsing every
    /// outcome into one failure. A success branch is checked against
    /// `metadata.version.api_version` first: a newer Core is a refusal, never a result
    /// read as a subset of itself. Only then is the payload handed to its own strict
    /// decoder, which is the one that judges its fields and vocabularies.
    static func decode<Value: Decodable>(_ output: Data) -> Result<Value, CoreHistoryError> {
        guard let document = try? JSONSerialization.jsonObject(with: output) as? [String: Any],
              let metadata = document["metadata"] as? [String: Any]
        else { return .failure(.unreadable) }
        let result = document["result"] as? [String: Any]
        let failure = document["error"] as? [String: Any]
        // Exactly one branch, and a refusal that names no code is not a refusal this
        // build can act on.
        guard (result == nil) != (failure == nil) else { return .failure(.unreadable) }
        if let failure {
            guard let code = failure["code"] as? String, !code.isEmpty,
                  code.unicodeScalars.count <= 128
            else { return .failure(.unreadable) }
            return .failure(.refused(code: code))
        }
        guard let version = metadata["version"] as? [String: Any],
              let apiVersion = version["api_version"] as? String,
              apiVersion == supportedCoreHistoryContractVersion,
              let result,
              let payload = try? JSONSerialization.data(withJSONObject: result, options: [])
        else { return .failure(.unreadable) }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard let value = try? decoder.decode(Value.self, from: payload) else {
            return .failure(.unreadable)
        }
        return .success(value)
    }
}

final class CoreHistoryLockedData: @unchecked Sendable {
    private let lock = NSLock()
    private var storage = Data()

    func replace(with data: Data) {
        lock.lock()
        storage = data
        lock.unlock()
    }

    func value() -> Data {
        lock.lock()
        defer { lock.unlock() }
        return storage
    }
}
