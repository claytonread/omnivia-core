import AppKit
import CryptoKit
import Darwin
import Foundation
import UniformTypeIdentifiers

/// Captures shared content as copies this Mac owns, durably, without needing
/// OmniVia Core to be open.
///
/// What this extension never does:
///
/// * claim an original. Every slot is a captured copy. `inPlace` is never
///   requested, a provider's file URL is copied rather than bookmarked, and
///   nothing shared here is ever monitored. Linking an original is a separate
///   explicit action that happens in the companion's own reviewed chooser.
/// * fetch anything. A shared link is captured as the text of the link; this
///   extension has no network entitlement and resolves nothing.
/// * report success it does not have. A selection is captured whole or not at
///   all, and once the request is durable the words are *queued*, never
///   *added*, with no destination or caller named.
/// * authenticate, or take the grant-bearing handoff's 120-second transfer
///   window. The spool it writes to carries no grant and has no lifetime.
@objc(ShareViewController)
final class ShareViewController: NSViewController {
    private let status = NSTextField(labelWithString: "Preparing captured copies…")
    private let detail = NSTextField(
        labelWithString: "OmniVia Core adds a copy. Your original stays where it is."
    )
    private let linkOriginalButton = NSButton(
        title: "Link an original…",
        target: nil,
        action: nil
    )
    private let dismissButton = NSButton(title: "Cancel", target: nil, action: nil)
    private var started = false
    private var inbox: ShareInboxStore?
    private var request: ShareInboxRequest?
    /// Once true the request is the person's, not the share sheet's: dismissing
    /// this panel must not take it back.
    private var published = false

    override func loadView() {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .centerX
        root.spacing = 12
        root.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        status.alignment = .center
        status.maximumNumberOfLines = 3
        detail.alignment = .center
        detail.maximumNumberOfLines = 2
        detail.textColor = .secondaryLabelColor
        detail.font = .systemFont(ofSize: 11)
        root.addArrangedSubview(status)
        root.addArrangedSubview(detail)
        linkOriginalButton.target = self
        linkOriginalButton.action = #selector(linkOriginalAction)
        linkOriginalButton.toolTip =
            "Opens OmniVia Core so you can choose the original file there. "
                + "A shared copy is never linked to its original."
        dismissButton.target = self
        dismissButton.action = #selector(dismissAction)
        let actions = NSStackView(views: [linkOriginalButton, dismissButton])
        actions.spacing = 8
        root.addArrangedSubview(actions)
        view = root
    }

    override func viewDidAppear() {
        super.viewDidAppear()
        guard !started else { return }
        started = true
        Task { @MainActor in await beginCapture() }
    }

    /// Cancel before the request is durable drops the bytes. After it, there is
    /// nothing to drop: the request stands and this panel simply closes.
    @objc private func dismissAction() {
        if published {
            extensionContext?.completeRequest(returningItems: [], completionHandler: nil)
            return
        }
        cleanUpUnpublished()
        extensionContext?.cancelRequest(withError: OmniViaExtensionError.invalidInput)
    }

    /// The truthful continuation for linking an original. Nothing here can take
    /// a durable grant on a provider's copy, so this extension does not pretend
    /// to: it asks the companion to open its own reviewed chooser and says so.
    @objc private func linkOriginalAction() {
        linkOriginalButton.isEnabled = false
        let store = inbox ?? (try? ShareInboxStore.appGroupInbox())
        guard let store, (try? store.requestChooser()) != nil else {
            status.stringValue = "Open OmniVia Core and choose the original there."
            return
        }
        try? activateContainingApp()
        status.stringValue = "Choose the original in OmniVia Core."
        detail.stringValue = "Nothing has been added yet."
    }

    @MainActor
    private func beginCapture() async {
        do {
            guard let context = extensionContext else { throw OmniViaExtensionError.invalidInput }
            let items = context.inputItems.compactMap { $0 as? NSExtensionItem }
            let providers = items.map { $0.attachments ?? [] }
            guard let choices = ShareRepresentationPolicy.slots(
                forItems: providers.map { $0.map(\.registeredTypeIdentifiers) }
            ), !choices.isEmpty, choices.count <= ShareInboxStore.maximumItems else {
                throw OmniViaExtensionError.invalidInput
            }

            let store = try ShareInboxStore.appGroupInbox()
            inbox = store
            let opened = try store.beginRequest()
            request = opened
            status.stringValue = "Copying shared content…"

            var captured: [ShareInboxItemV1] = []
            var aggregate: UInt64 = 0
            for (index, choice) in choices.enumerated() {
                let provider = providers[choice.itemIndex][choice.attachmentIndex]
                let relativePath = ShareInboxStore.payloadName(index: index)
                let destination = try store.payloadDestination(opened, relativePath: relativePath)
                let written = try await capture(
                    choice.representation,
                    from: provider,
                    to: destination,
                    remaining: ShareInboxStore.maximumSelectionBytes - aggregate
                )
                aggregate += written.byteLength
                captured.append(ShareInboxItemV1(
                    slotID: UUID().uuidString.lowercased(),
                    displayName: snapshotDisplayName(
                        provider.suggestedName,
                        representation: choice.representation,
                        index: index
                    ),
                    relativePath: relativePath,
                    kind: choice.representation.kind,
                    byteLength: written.byteLength,
                    digest: written.digest
                ))
            }
            try store.publish(ShareInboxEnvelopeV1(
                requestID: opened.requestID,
                senderBundleID: NativeHandoffMailbox.shareBundleID,
                createdAt: Date(),
                items: captured
            ))
            published = true
            dismissButton.title = "Done"
            status.stringValue = ShareInboxStore.queuedWording
            detail.stringValue = "\(captured.count) captured copy(ies). Nothing has been added yet."
            await openForReview(store: store, requestID: opened.requestID)
        } catch {
            // A failure after the atomic rename is an uncertain durability
            // acknowledgement, not permission to tell the person nothing was
            // queued. If the published directory is visible, preserve it and
            // report the conservative queued state; reconciliation can adopt
            // the same request idempotently.
            if let store = inbox,
               let request,
               store.isPending(requestID: request.requestID)
            {
                published = true
                dismissButton.title = "Done"
                status.stringValue = ShareInboxStore.queuedWording
                detail.stringValue = "Captured copies are waiting. Nothing has been added yet."
                await openForReview(store: store, requestID: request.requestID)
                return
            }
            cleanUpUnpublished()
            status.stringValue = Self.refusal(error)
            detail.stringValue = "Nothing was added or queued."
        }
    }

    /// One fixed activation attempt, then a bounded wait to see whether the
    /// companion took the request. Either outcome is truthful: the request is
    /// durable regardless, and no caller or destination is claimed.
    @MainActor
    private func openForReview(store: ShareInboxStore, requestID: String) async {
        try? activateContainingApp()
        let deadline = Date().addingTimeInterval(4)
        while Date() < deadline {
            guard store.isPending(requestID: requestID) else {
                status.stringValue = "Ready to review in OmniVia Core."
                detail.stringValue = "Nothing has been added yet."
                extensionContext?.completeRequest(returningItems: [], completionHandler: nil)
                return
            }
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
    }

    private func cleanUpUnpublished() {
        guard !published, let request else { return }
        inbox?.abandon(requestID: request.requestID)
        self.request = nil
    }

    private static func refusal(_ error: Error) -> String {
        switch error {
        case ShareInboxError.inboxFull:
            "This Mac is already holding the most it can of unsent shared content. "
                + "Open OmniVia Core to review it first."
        case ShareInboxError.selectionTooLarge, OmniViaExtensionError.selectionTooLarge:
            "That selection is too large to capture. Share fewer or smaller items."
        case OmniViaExtensionError.itemTooLarge, ShareInboxError.itemTooLarge:
            "One of those items is too large to capture. Share a smaller one."
        case OmniViaExtensionError.providerTimeout:
            "The sharing app stopped responding. Retry or cancel."
        default:
            "Couldn’t capture this shared content. Retry or cancel."
        }
    }
}

// MARK: Capturing one representation

private struct CapturedBytes {
    let byteLength: UInt64
    let digest: String
}

/// Resolves an NSItemProvider load once. A provider may answer after the
/// extension has timed out; that late callback is ignored instead of resuming
/// a Swift continuation twice or reviving an abandoned request.
private final class ProviderLoadGate<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Value, any Error>?

    init(_ continuation: CheckedContinuation<Value, any Error>) {
        self.continuation = continuation
    }

    func resolve(
        _ result: Result<Value, any Error>,
        beforeResume: () -> Void = {}
    ) {
        let continuation = lock.withLock { () -> CheckedContinuation<Value, any Error>? in
            defer { self.continuation = nil }
            return self.continuation
        }
        guard let continuation else { return }
        beforeResume()
        continuation.resume(with: result)
    }
}

private extension ShareViewController {
    static let providerLoadTimeout: TimeInterval = 30

    func boundedProviderLoad<Value>(
        onTimeout: @escaping () -> Void = {},
        _ start: (@escaping (Result<Value, any Error>) -> Void) -> Void
    ) async throws -> Value {
        try await withCheckedThrowingContinuation { continuation in
            let gate = ProviderLoadGate(continuation)
            DispatchQueue.global(qos: .userInitiated).asyncAfter(
                deadline: .now() + Self.providerLoadTimeout
            ) {
                gate.resolve(
                    .failure(OmniViaExtensionError.providerTimeout),
                    beforeResume: onTimeout
                )
            }
            start { result in gate.resolve(result) }
        }
    }

    /// Copies or writes one chosen representation's bytes into the staging
    /// directory. Every path produces a captured copy; none of them takes a
    /// bookmark, requests `inPlace`, or opens the network.
    func capture(
        _ representation: ShareRepresentation,
        from provider: NSItemProvider,
        to destination: URL,
        remaining: UInt64
    ) async throws -> CapturedBytes {
        switch representation.kind {
        case .file, .image:
            return try await copyProviderFile(
                from: provider,
                typeIdentifier: representation.typeIdentifier,
                destination: destination,
                remaining: remaining
            )
        case .text:
            let text = try await loadProviderText(
                from: provider,
                typeIdentifier: representation.typeIdentifier
            )
            return try writeCapturedBytes(Data(text.utf8), to: destination, remaining: remaining)
        case .link:
            let url = try await loadProviderURL(
                from: provider,
                typeIdentifier: representation.typeIdentifier
            )
            // The link is captured as its own text. It is not opened, resolved
            // or requested, here or later.
            guard ShareRepresentationPolicy.isCapturableLink(url) else {
                throw OmniViaExtensionError.invalidInput
            }
            return try writeCapturedBytes(
                Data(url.absoluteString.utf8),
                to: destination,
                remaining: remaining
            )
        }
    }

    /// Copies a provider's representation before the provider's lifetime ends.
    ///
    /// The temporary file the provider hands over is checked with `lstat`
    /// first: it must be a regular file, never a symlink or anything else, and
    /// it is read, hashed and written through rather than retained — once this
    /// returns, the copy stands on its own even though the source is gone.
    func copyProviderFile(
        from provider: NSItemProvider,
        typeIdentifier: String,
        destination: URL,
        remaining: UInt64
    ) async throws -> CapturedBytes {
        try await boundedProviderLoad(onTimeout: {
            try? FileManager.default.removeItem(at: destination)
        }) { finish in
            provider.loadFileRepresentation(forTypeIdentifier: typeIdentifier) { temporaryURL, error in
                do {
                    if let error { throw error }
                    guard let temporaryURL else { throw OmniViaExtensionError.invalidInput }
                    var source = stat()
                    guard Darwin.lstat(temporaryURL.path, &source) == 0,
                          source.st_mode & S_IFMT == S_IFREG
                    else { throw OmniViaExtensionError.invalidInput }
                    finish(.success(try Self.copyBounded(
                        from: temporaryURL,
                        to: destination,
                        remaining: remaining
                    )))
                } catch {
                    try? FileManager.default.removeItem(at: destination)
                    finish(.failure(error))
                }
            }
        }
    }

    static func copyBounded(
        from source: URL,
        to destination: URL,
        remaining: UInt64
    ) throws -> CapturedBytes {
        FileManager.default.createFile(
            atPath: destination.path,
            contents: nil,
            attributes: [.posixPermissions: 0o600]
        )
        guard let input = try? FileHandle(forReadingFrom: source),
              let output = try? FileHandle(forWritingTo: destination)
        else { throw OmniViaExtensionError.invalidInput }
        defer {
            try? input.close()
            try? output.close()
        }
        var hasher = SHA256()
        var copied: UInt64 = 0
        while true {
            let chunk = try input.read(upToCount: 1_024 * 1_024) ?? Data()
            if chunk.isEmpty { break }
            copied += UInt64(chunk.count)
            try Self.checkBounds(copied, remaining: remaining)
            hasher.update(data: chunk)
            try output.write(contentsOf: chunk)
        }
        try output.close()
        return CapturedBytes(byteLength: copied, digest: Self.hex(hasher.finalize()))
    }

    func writeCapturedBytes(
        _ data: Data,
        to destination: URL,
        remaining: UInt64
    ) throws -> CapturedBytes {
        try Self.checkBounds(UInt64(data.count), remaining: remaining)
        do {
            try data.write(to: destination, options: [.atomic])
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: destination.path
            )
        } catch {
            try? FileManager.default.removeItem(at: destination)
            throw error
        }
        return CapturedBytes(
            byteLength: UInt64(data.count),
            digest: ShareInboxStore.digest(of: data)
        )
    }

    static func checkBounds(_ bytes: UInt64, remaining: UInt64) throws {
        guard bytes <= ShareInboxStore.maximumItemBytes else {
            throw OmniViaExtensionError.itemTooLarge
        }
        guard bytes <= remaining else { throw OmniViaExtensionError.selectionTooLarge }
    }

    static func hex(_ digest: SHA256Digest) -> String {
        "sha256:" + digest.map { String(format: "%02x", $0) }.joined()
    }

    func loadProviderText(
        from provider: NSItemProvider,
        typeIdentifier: String
    ) async throws -> String {
        try await boundedProviderLoad { finish in
            provider.loadItem(forTypeIdentifier: typeIdentifier, options: nil) { item, error in
                if let error {
                    finish(.failure(error))
                } else if let text = item as? String {
                    finish(.success(text))
                } else if let attributed = item as? NSAttributedString {
                    finish(.success(attributed.string))
                } else if let data = item as? Data,
                          let text = String(data: data, encoding: .utf8)
                {
                    finish(.success(text))
                } else {
                    finish(.failure(OmniViaExtensionError.invalidInput))
                }
            }
        }
    }

    func loadProviderURL(
        from provider: NSItemProvider,
        typeIdentifier: String
    ) async throws -> URL {
        try await boundedProviderLoad { finish in
            provider.loadItem(forTypeIdentifier: typeIdentifier, options: nil) { item, error in
                if let error {
                    finish(.failure(error))
                } else if let url = item as? URL {
                    finish(.success(url))
                } else if let data = item as? Data,
                          let text = String(data: data, encoding: .utf8),
                          let url = URL(string: text)
                {
                    finish(.success(url))
                } else {
                    finish(.failure(OmniViaExtensionError.invalidInput))
                }
            }
        }
    }

    /// The name review will show, and the name the companion's own copy takes.
    /// It is a plain safe filename with an extension that says what the bytes
    /// are, and never a path, a hidden name or a provider-chosen component.
    func snapshotDisplayName(
        _ suggested: String?,
        representation: ShareRepresentation,
        index: Int
    ) -> String {
        let fallbackStem: String
        switch representation.kind {
        case .link: fallbackStem = "Shared Link \(index + 1)"
        case .text: fallbackStem = "Shared Text \(index + 1)"
        case .image: fallbackStem = "Shared Image \(index + 1)"
        case .file: fallbackStem = "Shared Item \(index + 1)"
        }
        let defaultExtension: String
        switch representation.kind {
        case .link, .text: defaultExtension = "txt"
        case .file, .image:
            defaultExtension = UTType(representation.typeIdentifier)?
                .preferredFilenameExtension ?? "dat"
        }
        let leaf = ((suggested ?? "") as NSString).lastPathComponent
            .replacingOccurrences(of: "\0", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        var stem = (leaf as NSString).deletingPathExtension
            .trimmingCharacters(in: CharacterSet(charactersIn: "."))
        // Text and link captures are always written as the UTF-8 text they
        // are, so a suggested extension never decides what they claim to be.
        var suffix = representation.kind == .link || representation.kind == .text
            ? defaultExtension
            : (leaf as NSString).pathExtension
        if stem.isEmpty { stem = fallbackStem }
        if suffix.isEmpty || suffix.utf8.count > 32 { suffix = defaultExtension }
        let name = "\(stem).\(suffix)"
        // A provider-chosen name that is not a plain, short, safe component is
        // replaced outright rather than trimmed into something it never was.
        return OwnedSnapshotStore.isSafeComponent(name) && name.utf8.count <= 200
            ? name
            : "\(fallbackStem).\(defaultExtension)"
    }
}
