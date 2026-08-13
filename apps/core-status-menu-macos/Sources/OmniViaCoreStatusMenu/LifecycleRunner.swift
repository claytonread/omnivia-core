import Foundation

/// The closed set of ways this companion ends up with no safe status to show.
/// Deliberately carries no payload: a launcher message, a decoder complaint or
/// a line of the adapter's stderr is exactly what must not reach the menu, so
/// there is nowhere here to put one.
enum LifecycleRunnerError: Error, Equatable, Sendable {
    case commandUnavailable
    case timedOut
    case unreadableOutput
    case actionMismatch
    case exitStatusMismatch
    case noSafeStatus
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
    private let installationState: URL
    private let workspaceID: String
    private let worker: DispatchQueue
    private let callbackQueue: DispatchQueue

    init(
        executable: URL,
        installationState: URL,
        workspaceID: String,
        worker: DispatchQueue = DispatchQueue(
            label: "com.omnivia.core-status-menu.commands",
            qos: .userInitiated
        ),
        callbackQueue: DispatchQueue = .main
    ) {
        self.executable = executable
        self.installationState = installationState
        self.workspaceID = workspaceID
        self.worker = worker
        self.callbackQueue = callbackQueue
    }

    func run(
        _ action: LifecycleAction,
        completion: @escaping LifecycleCompletion
    ) {
        worker.async { [executable, installationState, workspaceID, callbackQueue] in
            let invocation = CLIInvocation.lifecycle(
                executable: executable,
                installationState: installationState,
                workspaceID: workspaceID,
                action: action
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
                    completion(.failure(.commandUnavailable))
                }
                return
            }

            let output = LockedData()
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
                // Drained so a chatty adapter cannot deadlock on a full pipe,
                // and discarded on the spot: human stderr is not this
                // companion's to read, let alone to show.
                _ = try? standardError.fileHandleForReading.readToEnd()
                readers.leave()
            }

            let deadline = DispatchTime.now() + Self.timeout(for: action)
            if terminated.wait(timeout: deadline) == .timedOut {
                process.terminate()
                _ = terminated.wait(timeout: .now() + 2)
                _ = readers.wait(timeout: .now() + 2)
                callbackQueue.async {
                    completion(.failure(.timedOut))
                }
                return
            }

            _ = readers.wait(timeout: .now() + 2)
            let result = Self.decode(
                action: action,
                terminationStatus: process.terminationStatus,
                output: output.value()
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
        output: Data
    ) -> Result<LifecycleDocument, LifecycleRunnerError> {
        let document: LifecycleDocument
        do {
            document = try LifecycleDocument.decode(output)
        } catch {
            // The decoder's own complaint names the field, the value and the
            // path it walked. None of that is shown; the refusal is the whole
            // of what the menu learns.
            return .failure(.unreadableOutput)
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

/// Stands in when no `omnivia` executable could be resolved. The resolution
/// error named the path it rejected, so it is dropped here rather than carried.
final class UnavailableLifecycleRunner: LifecycleRunning, @unchecked Sendable {
    func run(
        _ action: LifecycleAction,
        completion: @escaping LifecycleCompletion
    ) {
        completion(.failure(.commandUnavailable))
    }
}
