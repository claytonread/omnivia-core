import Foundation

public enum NativePeerRequirementError: Error, Equatable {
    case invalidRequirement
}

/// Applies the operating system's code-signing check before an XPC peer may send a grant.
/// The requirement is release configuration, never input supplied by the connecting process.
public struct NativePeerRequirement: Sendable, Equatable {
    public let value: String

    public init(_ value: String) throws {
        guard !value.isEmpty, value.utf8.count <= 2_048, !value.contains("\0") else {
            throw NativePeerRequirementError.invalidRequirement
        }
        self.value = value
    }

    public func apply(to connection: NSXPCConnection) {
        connection.setCodeSigningRequirement(value)
    }

    public func apply(to listener: NSXPCListener) {
        listener.setConnectionCodeSigningRequirement(value)
    }
}
