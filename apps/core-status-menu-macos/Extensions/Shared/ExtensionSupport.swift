import AppKit
import Darwin
import Foundation

enum OmniViaExtensionError: Error {
    case invalidInput
    case companionUnavailable
    case acknowledgementTimeout
    case providerTimeout
    case itemTooLarge
    case selectionTooLarge
}

@MainActor
func containingAppURL() throws -> URL {
    let appURL = Bundle.main.bundleURL
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    guard appURL.pathExtension == "app",
          Bundle(url: appURL)?.bundleIdentifier == NativeHandoffMailbox.companionBundleID
    else { throw OmniViaExtensionError.companionUnavailable }
    return appURL
}

@MainActor
func activateContainingApp() throws {
    let configuration = NSWorkspace.OpenConfiguration()
    configuration.activates = true
    configuration.createsNewApplicationInstance = false
    NSWorkspace.shared.openApplication(
        at: try containingAppURL(),
        configuration: configuration,
        completionHandler: { _, _ in }
    )
}

@MainActor
func submitAuthenticatedOffer(
    _ offer: NativeHandoffOfferV1,
    mailbox: NativeHandoffMailbox,
    timeout: TimeInterval = 30
) async throws -> NativeHandoffAcknowledgementV1 {
    let appURL = try containingAppURL()
    let requirement = try NativePeerRequirement.designatedRequirement(
        for: appURL,
        expectedBundleID: NativeHandoffMailbox.companionBundleID
    )
    let encoded = try mailbox.encodedOffer(offer)
    try activateContainingApp()
    let socketURL = mailbox.peerSocketURL
    let acknowledgementData = try await Task.detached(priority: .userInitiated) {
        let deadline = Date().addingTimeInterval(timeout)
        var descriptor: Int32?
        while descriptor == nil, Date() < deadline {
            do {
                descriptor = try NativePeerSocket.connect(to: socketURL)
            } catch NativePeerSocketError.unavailable {
                try await Task.sleep(nanoseconds: 100_000_000)
            }
        }
        guard let descriptor else {
            throw OmniViaExtensionError.companionUnavailable
        }
        defer { Darwin.close(descriptor) }

        let identity = try NativePeerSocket.peerIdentity(of: descriptor)
        guard identity.userID == geteuid() else {
            throw NativePeerRequirementError.peerRejected
        }
        try requirement.validate(processID: identity.processID)
        try NativePeerSocket.writeFrame(
            encoded,
            to: descriptor,
            maximumBytes: 4 * 1_024 * 1_024
        )
        let response = try NativePeerSocket.readFrame(
            from: descriptor,
            maximumBytes: 256 * 1_024 + 1
        )
        guard response.first == 0 else {
            throw OmniViaExtensionError.invalidInput
        }
        return Data(response.dropFirst())
    }.value
    return try mailbox.decodeAcknowledgement(
        acknowledgementData,
        expectedNonce: offer.requestNonce
    )
}

func loadFileURL(from provider: NSItemProvider) async throws -> URL {
    try await withCheckedThrowingContinuation { continuation in
        provider.loadItem(forTypeIdentifier: "public.file-url", options: nil) { item, error in
            if let error {
                continuation.resume(throwing: error)
            } else if let url = item as? URL {
                continuation.resume(returning: url)
            } else if let data = item as? Data,
                      let string = String(data: data, encoding: .utf8),
                      let url = URL(string: string),
                      url.isFileURL
            {
                continuation.resume(returning: url)
            } else {
                continuation.resume(throwing: OmniViaExtensionError.invalidInput)
            }
        }
    }
}
