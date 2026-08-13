import Darwin
import Foundation

enum CompanionSingletonError: Error, Equatable {
    case stateDirectoryUnavailable
    case lockUnavailable
    case activationUnavailable
    case socketPathTooLong
}

enum CompanionSingletonAcquisition {
    case primary(CompanionSingleton)
    case forwarded
}

/// One companion per user/installation, guarded by an advisory lock and an
/// owner-only Unix activation socket. The socket accepts one fixed intent and
/// carries no path, credential, workspace authority or arbitrary payload.
final class CompanionSingleton: @unchecked Sendable {
    static let activationIntent = Data("refresh\n".utf8)
    private static let registryLock = NSLock()
    private static var processOwnedLocks = Set<String>()

    private let lockDescriptor: Int32
    private let listenerDescriptor: Int32
    private let socketURL: URL
    private let lockPath: String
    private let queue = DispatchQueue(label: "com.omnivia.core.status.activation")
    private let stateLock = NSLock()
    private var listening = false
    private var closed = false

    private init(lockDescriptor: Int32, listenerDescriptor: Int32, socketURL: URL, lockPath: String) {
        self.lockDescriptor = lockDescriptor
        self.listenerDescriptor = listenerDescriptor
        self.socketURL = socketURL
        self.lockPath = lockPath
    }

    deinit {
        close()
    }

    static func acquire(
        installationState: URL,
        retryCount: Int = 40,
        retryDelayMicroseconds: useconds_t = 25_000
    ) throws -> CompanionSingletonAcquisition {
        let directory = installationState
            .appendingPathComponent("companion", isDirectory: true)
        do {
            try FileManager.default.createDirectory(
                at: directory,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o700],
                ofItemAtPath: directory.path
            )
        } catch {
            throw CompanionSingletonError.stateDirectoryUnavailable
        }

        let lockURL = directory.appendingPathComponent("status-menu.lock")
        let socketURL = directory.appendingPathComponent("status-menu.sock")
        registryLock.lock()
        let alreadyOwnedInProcess = processOwnedLocks.contains(lockURL.path)
        registryLock.unlock()
        if alreadyOwnedInProcess {
            for _ in 0..<max(1, retryCount) {
                if forwardActivation(to: socketURL) { return .forwarded }
                Darwin.usleep(retryDelayMicroseconds)
            }
            throw CompanionSingletonError.activationUnavailable
        }
        let lockDescriptor = Darwin.open(lockURL.path, O_CREAT | O_RDWR | O_CLOEXEC, 0o600)
        guard lockDescriptor >= 0 else { throw CompanionSingletonError.lockUnavailable }
        _ = Darwin.fchmod(lockDescriptor, 0o600)

        guard Darwin.lockf(lockDescriptor, F_TLOCK, 0) == 0 else {
            Darwin.close(lockDescriptor)
            for _ in 0..<max(1, retryCount) {
                if forwardActivation(to: socketURL) {
                    return .forwarded
                }
                Darwin.usleep(retryDelayMicroseconds)
            }
            throw CompanionSingletonError.activationUnavailable
        }

        do {
            let listener = try makeListener(at: socketURL)
            registryLock.lock()
            processOwnedLocks.insert(lockURL.path)
            registryLock.unlock()
            return .primary(
                CompanionSingleton(
                    lockDescriptor: lockDescriptor,
                    listenerDescriptor: listener,
                    socketURL: socketURL,
                    lockPath: lockURL.path
                )
            )
        } catch {
            _ = Darwin.lockf(lockDescriptor, F_ULOCK, 0)
            Darwin.close(lockDescriptor)
            throw error
        }
    }

    func startListening(onActivation: @escaping @Sendable () -> Void) {
        stateLock.lock()
        guard !listening, !closed else {
            stateLock.unlock()
            return
        }
        listening = true
        stateLock.unlock()

        queue.async { [weak self] in
            guard let self else { return }
            while true {
                self.stateLock.lock()
                let shouldStop = self.closed
                self.stateLock.unlock()
                if shouldStop { return }

                let client = Darwin.accept(self.listenerDescriptor, nil, nil)
                if client < 0 {
                    if errno == EINTR { continue }
                    return
                }
                var bytes = [UInt8](repeating: 0, count: Self.activationIntent.count)
                let count = bytes.withUnsafeMutableBytes { buffer in
                    Darwin.read(client, buffer.baseAddress, buffer.count)
                }
                Darwin.close(client)
                if count == Self.activationIntent.count,
                   Data(bytes) == Self.activationIntent
                {
                    DispatchQueue.main.async(execute: onActivation)
                }
            }
        }
    }

    func close() {
        stateLock.lock()
        guard !closed else {
            stateLock.unlock()
            return
        }
        closed = true
        stateLock.unlock()

        Darwin.shutdown(listenerDescriptor, SHUT_RDWR)
        Darwin.close(listenerDescriptor)
        _ = Darwin.unlink(socketURL.path)
        _ = Darwin.lockf(lockDescriptor, F_ULOCK, 0)
        Darwin.close(lockDescriptor)
        Self.registryLock.lock()
        Self.processOwnedLocks.remove(lockPath)
        Self.registryLock.unlock()
    }

    private static func makeListener(at socketURL: URL) throws -> Int32 {
        let path = socketURL.path
        guard path.utf8.count + 1 <= MemoryLayout<sockaddr_un>.size - 2 else {
            throw CompanionSingletonError.socketPathTooLong
        }
        _ = Darwin.unlink(path)
        let descriptor = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw CompanionSingletonError.activationUnavailable }
        _ = Darwin.fcntl(descriptor, F_SETFD, FD_CLOEXEC)

        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(path.utf8CString)
        withUnsafeMutablePointer(to: &address.sun_path) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: pathBytes.count) { target in
                for (offset, byte) in pathBytes.enumerated() {
                    target[offset] = byte
                }
            }
        }
        let length = socklen_t(MemoryLayout<sa_family_t>.size + pathBytes.count)
        let bound = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(descriptor, $0, length)
            }
        }
        guard bound == 0, Darwin.chmod(path, 0o600) == 0, Darwin.listen(descriptor, 8) == 0 else {
            Darwin.close(descriptor)
            _ = Darwin.unlink(path)
            throw CompanionSingletonError.activationUnavailable
        }
        return descriptor
    }

    private static func forwardActivation(to socketURL: URL) -> Bool {
        let path = socketURL.path
        guard path.utf8.count + 1 <= MemoryLayout<sockaddr_un>.size - 2 else { return false }
        let descriptor = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else { return false }
        defer { Darwin.close(descriptor) }

        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(path.utf8CString)
        withUnsafeMutablePointer(to: &address.sun_path) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: pathBytes.count) { target in
                for (offset, byte) in pathBytes.enumerated() {
                    target[offset] = byte
                }
            }
        }
        let length = socklen_t(MemoryLayout<sa_family_t>.size + pathBytes.count)
        let connected = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(descriptor, $0, length)
            }
        }
        guard connected == 0 else { return false }
        return Self.activationIntent.withUnsafeBytes { buffer in
            Darwin.write(descriptor, buffer.baseAddress, buffer.count) == buffer.count
        }
    }
}
