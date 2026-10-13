import Darwin
import Foundation
import Security

public enum NativePeerRequirementError: Error, Equatable {
    case invalidRequirement
    case peerRejected
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

    /// Returns the exact designated requirement of an already installed peer.
    /// The caller separately binds the expected bundle identifier so a validly
    /// signed but unrelated bundle can never become the configured peer.
    public static func designatedRequirement(
        for bundleURL: URL,
        expectedBundleID: String
    ) throws -> NativePeerRequirement {
        guard bundleURL.isFileURL,
              Bundle(url: bundleURL)?.bundleIdentifier == expectedBundleID
        else { throw NativePeerRequirementError.invalidRequirement }
        var staticCode: SecStaticCode?
        guard SecStaticCodeCreateWithPath(
            bundleURL as CFURL,
            SecCSFlags(rawValue: 0),
            &staticCode
        ) == errSecSuccess,
              let staticCode,
              SecStaticCodeCheckValidity(staticCode, SecCSFlags(rawValue: 0), nil) == errSecSuccess
        else { throw NativePeerRequirementError.invalidRequirement }
        var requirement: SecRequirement?
        guard SecCodeCopyDesignatedRequirement(
            staticCode,
            SecCSFlags(rawValue: 0),
            &requirement
        ) == errSecSuccess,
              let requirement
        else { throw NativePeerRequirementError.invalidRequirement }
        var text: CFString?
        guard SecRequirementCopyString(
            requirement,
            SecCSFlags(rawValue: 0),
            &text
        ) == errSecSuccess,
              let text
        else { throw NativePeerRequirementError.invalidRequirement }
        return try NativePeerRequirement(text as String)
    }

    public static func any(of requirements: [NativePeerRequirement]) throws -> NativePeerRequirement {
        guard !requirements.isEmpty else { throw NativePeerRequirementError.invalidRequirement }
        return try NativePeerRequirement(
            requirements.map { "(\($0.value))" }.joined(separator: " or ")
        )
    }

    /// Validates the kernel-reported process identity against this designated
    /// requirement. The PID must come from the connected socket, never payload.
    public func validate(processID: pid_t) throws {
        guard processID > 0 else { throw NativePeerRequirementError.peerRejected }
        let attributes = [kSecGuestAttributePid: NSNumber(value: processID)] as CFDictionary
        var code: SecCode?
        guard SecCodeCopyGuestWithAttributes(
            nil,
            attributes,
            SecCSFlags(rawValue: 0),
            &code
        ) == errSecSuccess,
              let code
        else { throw NativePeerRequirementError.peerRejected }
        var requirement: SecRequirement?
        guard SecRequirementCreateWithString(
            value as CFString,
            SecCSFlags(rawValue: 0),
            &requirement
        ) == errSecSuccess,
              let requirement,
              SecCodeCheckValidity(code, SecCSFlags(rawValue: 0), requirement) == errSecSuccess
        else { throw NativePeerRequirementError.peerRejected }
    }
}
