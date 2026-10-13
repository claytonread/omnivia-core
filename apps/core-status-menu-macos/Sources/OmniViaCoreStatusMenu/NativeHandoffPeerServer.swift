import Darwin
import DocumentIntakeMacOS
import Foundation

private final class NativeHandoffResponseBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Data?

    func store(_ value: Data) { lock.withLock { self.value = value } }
    func load() -> Data? { lock.withLock { value } }
}

/// Authenticated grant handoff over an owner-only App Group Unix socket. The
/// kernel supplies peer UID/PID; Security.framework validates that process
/// against the exact installed extension designated requirements.
final class NativeHandoffPeerServer: @unchecked Sendable {
    private static let maximumOfferBytes = 4 * 1_024 * 1_024
    private static let maximumResponseBytes = 256 * 1_024 + 1

    private let listenerDescriptor: Int32
    private let socketURL: URL
    private let receiver: NativeHandoffReceiver
    private let acceptedPeers: NativePeerRequirement
    private let acceptQueue = DispatchQueue(label: "com.omnivia.core.native-handoff.accept")
    private let workerQueue = DispatchQueue(
        label: "com.omnivia.core.native-handoff.worker",
        attributes: .concurrent
    )
    private let stateLock = NSLock()
    private var closed = false

    init(
        rendezvous: NativeHandoffMailbox,
        receiver: NativeHandoffReceiver,
        companionBundleURL: URL
    ) throws {
        let plugins = companionBundleURL
            .appendingPathComponent("Contents", isDirectory: true)
            .appendingPathComponent("PlugIns", isDirectory: true)
        let finder = try NativePeerRequirement.designatedRequirement(
            for: plugins.appendingPathComponent(
                NativeHandoffMailbox.finderBundleName,
                isDirectory: true
            ),
            expectedBundleID: NativeHandoffMailbox.finderBundleID
        )
        let share = try NativePeerRequirement.designatedRequirement(
            for: plugins.appendingPathComponent(
                NativeHandoffMailbox.shareBundleName,
                isDirectory: true
            ),
            expectedBundleID: NativeHandoffMailbox.shareBundleID
        )
        acceptedPeers = try NativePeerRequirement.any(of: [finder, share])
        socketURL = rendezvous.peerSocketURL
        listenerDescriptor = try NativePeerSocket.makeListener(at: socketURL)
        self.receiver = receiver
        start()
    }

    deinit { close() }

    func close() {
        let shouldClose = stateLock.withLock {
            guard !closed else { return false }
            closed = true
            return true
        }
        guard shouldClose else { return }
        Darwin.shutdown(listenerDescriptor, SHUT_RDWR)
        Darwin.close(listenerDescriptor)
        _ = Darwin.unlink(socketURL.path)
    }

    private func start() {
        acceptQueue.async { [weak self] in
            guard let self else { return }
            while !self.stateLock.withLock({ self.closed }) {
                let client = Darwin.accept(self.listenerDescriptor, nil, nil)
                if client < 0 {
                    if errno == EINTR { continue }
                    return
                }
                self.workerQueue.async { [weak self] in self?.handle(client) }
            }
        }
    }

    private func handle(_ descriptor: Int32) {
        defer { Darwin.close(descriptor) }
        do {
            let identity = try NativePeerSocket.peerIdentity(of: descriptor)
            guard identity.userID == geteuid() else {
                throw NativePeerRequirementError.peerRejected
            }
            try acceptedPeers.validate(processID: identity.processID)
            let request = try NativePeerSocket.readFrame(
                from: descriptor,
                maximumBytes: Self.maximumOfferBytes
            )
            let response = NativeHandoffResponseBox()
            let completed = DispatchSemaphore(value: 0)
            Task { @MainActor [receiver] in
                do {
                    var payload = Data([0])
                    payload.append(try await receiver.submitAuthenticatedOffer(request))
                    response.store(payload)
                } catch {
                    response.store(Data([1]))
                }
                completed.signal()
            }
            guard completed.wait(timeout: .now() + 35) == .success,
                  let payload = response.load()
            else { throw NativePeerSocketError.unavailable }
            try NativePeerSocket.writeFrame(
                payload,
                to: descriptor,
                maximumBytes: Self.maximumResponseBytes
            )
        } catch {
            try? NativePeerSocket.writeFrame(
                Data([1]),
                to: descriptor,
                maximumBytes: Self.maximumResponseBytes
            )
        }
    }
}
