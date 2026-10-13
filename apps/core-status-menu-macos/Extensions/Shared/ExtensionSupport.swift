import AppKit
import Foundation

enum OmniViaExtensionError: Error {
    case invalidInput
    case companionUnavailable
    case acknowledgementTimeout
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

func waitForAcknowledgement(
    mailbox: NativeHandoffMailbox,
    nonce: String,
    timeout: TimeInterval = 30
) async throws -> NativeHandoffAcknowledgementV1 {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if let acknowledgement = mailbox.acknowledgement(nonce: nonce) {
            return acknowledgement
        }
        try await Task.sleep(nanoseconds: 200_000_000)
    }
    throw OmniViaExtensionError.acknowledgementTimeout
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

func copyProviderFile(
    from provider: NSItemProvider,
    typeIdentifier: String,
    destination: URL,
    maximumBytes: UInt64
) async throws -> UInt64 {
    try await withCheckedThrowingContinuation { continuation in
        provider.loadFileRepresentation(forTypeIdentifier: typeIdentifier) { temporaryURL, error in
            do {
                if let error { throw error }
                guard let temporaryURL else { throw OmniViaExtensionError.invalidInput }
                FileManager.default.createFile(atPath: destination.path, contents: nil)
                let input = try FileHandle(forReadingFrom: temporaryURL)
                let output = try FileHandle(forWritingTo: destination)
                defer {
                    try? input.close()
                    try? output.close()
                }
                var copied: UInt64 = 0
                while true {
                    let chunk = try input.read(upToCount: 1_024 * 1_024) ?? Data()
                    if chunk.isEmpty { break }
                    copied += UInt64(chunk.count)
                    guard copied <= maximumBytes else { throw OmniViaExtensionError.itemTooLarge }
                    try output.write(contentsOf: chunk)
                }
                try FileManager.default.setAttributes(
                    [.posixPermissions: 0o600],
                    ofItemAtPath: destination.path
                )
                continuation.resume(returning: copied)
            } catch {
                try? FileManager.default.removeItem(at: destination)
                continuation.resume(throwing: error)
            }
        }
    }
}

func safeProviderFilename(_ proposed: String, index: Int) throws -> String {
    let leaf = (proposed as NSString).lastPathComponent
        .replacingOccurrences(of: "\0", with: "")
        .trimmingCharacters(in: .whitespacesAndNewlines)
    guard !leaf.isEmpty, leaf != ".", leaf != "..", !leaf.contains("/") else {
        throw OmniViaExtensionError.invalidInput
    }
    return "\(index + 1)-\(UUID().uuidString.lowercased())-\(leaf)"
}
