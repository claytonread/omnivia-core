import Darwin
import Foundation

public enum NativePeerSocketError: Error, Equatable {
    case pathTooLong
    case unavailable
    case peerUnavailable
    case frameTooLarge
    case truncatedFrame
}

public struct NativePeerSocketIdentity: Equatable, Sendable {
    public let userID: uid_t
    public let processID: pid_t
}

/// Bounded framing and kernel peer identity for the authenticated native
/// handoff rendezvous. Payload identity claims are never consulted.
public enum NativePeerSocket {
    public static func makeListener(at socketURL: URL) throws -> Int32 {
        let descriptor = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw NativePeerSocketError.unavailable }
        do {
            try configure(descriptor)
            var address = try socketAddress(socketURL.path)
            _ = Darwin.unlink(socketURL.path)
            let result = withUnsafePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.bind(descriptor, $0, socketLength(socketURL.path))
                }
            }
            guard result == 0,
                  Darwin.chmod(socketURL.path, 0o600) == 0,
                  Darwin.listen(descriptor, 8) == 0
            else { throw NativePeerSocketError.unavailable }
            return descriptor
        } catch {
            Darwin.close(descriptor)
            _ = Darwin.unlink(socketURL.path)
            throw error
        }
    }

    public static func connect(to socketURL: URL) throws -> Int32 {
        let descriptor = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw NativePeerSocketError.unavailable }
        do {
            try configure(descriptor)
            var address = try socketAddress(socketURL.path)
            let result = withUnsafePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.connect(descriptor, $0, socketLength(socketURL.path))
                }
            }
            guard result == 0 else { throw NativePeerSocketError.unavailable }
            return descriptor
        } catch {
            Darwin.close(descriptor)
            throw error
        }
    }

    public static func peerIdentity(of descriptor: Int32) throws -> NativePeerSocketIdentity {
        var userID: uid_t = 0
        var groupID: gid_t = 0
        guard Darwin.getpeereid(descriptor, &userID, &groupID) == 0 else {
            throw NativePeerSocketError.peerUnavailable
        }
        var processID: pid_t = 0
        var length = socklen_t(MemoryLayout<pid_t>.size)
        guard withUnsafeMutablePointer(to: &processID, { pointer in
            Darwin.getsockopt(descriptor, SOL_LOCAL, LOCAL_PEERPID, pointer, &length)
        }) == 0,
              processID > 0
        else { throw NativePeerSocketError.peerUnavailable }
        return NativePeerSocketIdentity(userID: userID, processID: processID)
    }

    public static func writeFrame(
        _ data: Data,
        to descriptor: Int32,
        maximumBytes: Int
    ) throws {
        guard data.count <= maximumBytes, data.count <= Int(UInt32.max) else {
            throw NativePeerSocketError.frameTooLarge
        }
        var length = UInt32(data.count).bigEndian
        try withUnsafeBytes(of: &length) { try writeAll($0, to: descriptor) }
        try data.withUnsafeBytes { try writeAll($0, to: descriptor) }
    }

    public static func readFrame(
        from descriptor: Int32,
        maximumBytes: Int
    ) throws -> Data {
        var encodedLength: UInt32 = 0
        try withUnsafeMutableBytes(of: &encodedLength) { try readAll($0, from: descriptor) }
        let count = Int(UInt32(bigEndian: encodedLength))
        guard count <= maximumBytes else { throw NativePeerSocketError.frameTooLarge }
        var data = Data(count: count)
        try data.withUnsafeMutableBytes { try readAll($0, from: descriptor) }
        return data
    }

    private static func configure(_ descriptor: Int32) throws {
        guard Darwin.fcntl(descriptor, F_SETFD, FD_CLOEXEC) == 0 else {
            throw NativePeerSocketError.unavailable
        }
        var enabled: Int32 = 1
        guard Darwin.setsockopt(
            descriptor,
            SOL_SOCKET,
            SO_NOSIGPIPE,
            &enabled,
            socklen_t(MemoryLayout<Int32>.size)
        ) == 0 else { throw NativePeerSocketError.unavailable }
    }

    private static func socketAddress(_ path: String) throws -> sockaddr_un {
        let bytes = Array(path.utf8CString)
        guard bytes.count <= MemoryLayout<sockaddr_un>.size - MemoryLayout<sa_family_t>.size else {
            throw NativePeerSocketError.pathTooLong
        }
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutablePointer(to: &address.sun_path) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: bytes.count) { target in
                for (offset, byte) in bytes.enumerated() { target[offset] = byte }
            }
        }
        return address
    }

    private static func socketLength(_ path: String) -> socklen_t {
        socklen_t(MemoryLayout<sa_family_t>.size + path.utf8CString.count)
    }

    private static func writeAll(_ buffer: UnsafeRawBufferPointer, to descriptor: Int32) throws {
        var offset = 0
        while offset < buffer.count {
            let written = Darwin.write(
                descriptor,
                buffer.baseAddress?.advanced(by: offset),
                buffer.count - offset
            )
            if written < 0, errno == EINTR { continue }
            guard written > 0 else { throw NativePeerSocketError.truncatedFrame }
            offset += written
        }
    }

    private static func readAll(
        _ buffer: UnsafeMutableRawBufferPointer,
        from descriptor: Int32
    ) throws {
        var offset = 0
        while offset < buffer.count {
            let read = Darwin.read(
                descriptor,
                buffer.baseAddress?.advanced(by: offset),
                buffer.count - offset
            )
            if read < 0, errno == EINTR { continue }
            guard read > 0 else { throw NativePeerSocketError.truncatedFrame }
            offset += read
        }
    }
}
