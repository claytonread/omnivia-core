import Foundation

enum LifecycleRunnerError: LocalizedError, Equatable, Sendable {
    case launchFailed(String)
    case timedOut(LifecycleAction)
    case invalidOutput(String)
    case actionMismatch
    case exitStatusMismatch

    var errorDescription: String? {
        switch self {
        case let .launchFailed(reason):
            return "could not launch OmniVia Core: \(reason)"
        case let .timedOut(action):
            return "OmniVia Core \(action.rawValue) timed out"
        case let .invalidOutput(reason):
            return reason
        case .actionMismatch:
            return "Core returned a lifecycle result for a different action"
        case .exitStatusMismatch:
            return "Core lifecycle result disagreed with the process exit status"
        }
    }
}

typealias LifecycleCompletion = @Sendable (
    Result<LifecycleDocument, LifecycleRunnerError>
) -> Void

protocol LifecycleRunning: AnyObject, Sendable {
    func run(
        _ action: LifecycleAction,
        completion: @escaping LifecycleCompletion
    )
}

private final class LockedData: @unchecked Sendable {
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

final class CLICommandRunner: LifecycleRunning, @unchecked Sendable {
    private let executable: URL
    private let home: URL?
    private let worker: DispatchQueue
    private let callbackQueue: DispatchQueue

    init(
        executable: URL,
        home: URL?,
        worker: DispatchQueue = DispatchQueue(
            label: "com.omnivia.core-status-menu.commands",
            qos: .userInitiated
        ),
        callbackQueue: DispatchQueue = .main
    ) {
        self.executable = executable
        self.home = home
        self.worker = worker
        self.callbackQueue = callbackQueue
    }

    func run(
        _ action: LifecycleAction,
        completion: @escaping LifecycleCompletion
    ) {
        worker.async { [executable, home, callbackQueue] in
            let invocation = CLIInvocation.lifecycle(
                executable: executable, home: home, action: action
            )
            let process = Process()
            let standardOutput = Pipe()
            let standardError = Pipe()
            let terminated = DispatchSemaphore(value: 0)

            process.executableURL = invocation.executable
            process.arguments = invocation.arguments
            process.standardOutput = standardOutput
            process.standardError = standardError
            process.terminationHandler = { _ in terminated.signal() }

            do {
                try process.run()
            } catch {
                callbackQueue.async {
                    completion(.failure(.launchFailed(error.localizedDescription)))
                }
                return
            }

            let output = LockedData()
            let errors = LockedData()
            let readers = DispatchGroup()
            readers.enter()
            DispatchQueue.global(qos: .utility).async {
                output.replace(
                    with: (try? standardOutput.fileHandleForReading.readToEnd()) ?? Data()
                )
                readers.leave()
            }
            readers.enter()
            DispatchQueue.global(qos: .utility).async {
                errors.replace(
                    with: (try? standardError.fileHandleForReading.readToEnd()) ?? Data()
                )
                readers.leave()
            }

            let deadline = DispatchTime.now() + Self.timeout(for: action)
            if terminated.wait(timeout: deadline) == .timedOut {
                process.terminate()
                _ = terminated.wait(timeout: .now() + 2)
                _ = readers.wait(timeout: .now() + 2)
                callbackQueue.async {
                    completion(.failure(.timedOut(action)))
                }
                return
            }

            _ = readers.wait(timeout: .now() + 2)
            let result = Self.decode(
                action: action,
                terminationStatus: process.terminationStatus,
                output: output.value(),
                errorOutput: errors.value()
            )
            callbackQueue.async { completion(result) }
        }
    }

    private static func timeout(for action: LifecycleAction) -> DispatchTimeInterval {
        switch action {
        case .status:
            return .seconds(12)
        case .start:
            return .seconds(100)
        case .stop:
            return .seconds(40)
        }
    }

    static func decode(
        action: LifecycleAction,
        terminationStatus: Int32,
        output: Data,
        errorOutput: Data
    ) -> Result<LifecycleDocument, LifecycleRunnerError> {
        let document: LifecycleDocument
        do {
            document = try LifecycleDocument.decode(output)
        } catch {
            let stderr = MenuProjection.displayReason(
                String(data: errorOutput, encoding: .utf8) ?? ""
            )
            let reason = stderr == "Core lifecycle command failed"
                ? "Core returned invalid lifecycle JSON"
                : stderr
            return .failure(.invalidOutput(reason))
        }
        guard document.action == action else {
            return .failure(.actionMismatch)
        }
        guard document.ok == (terminationStatus == 0) else {
            return .failure(.exitStatusMismatch)
        }
        return .success(document)
    }
}

final class UnavailableLifecycleRunner: LifecycleRunning, @unchecked Sendable {
    private let reason: String

    init(reason: String) {
        self.reason = reason
    }

    func run(
        _ action: LifecycleAction,
        completion: @escaping LifecycleCompletion
    ) {
        completion(.failure(.launchFailed(reason)))
    }
}
