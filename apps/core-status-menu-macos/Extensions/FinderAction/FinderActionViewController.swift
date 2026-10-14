import AppKit
import Foundation

@objc(FinderActionViewController)
final class FinderActionViewController: NSViewController {
    private let status = NSTextField(labelWithString: "Opening Add to Core…")
    private var started = false
    private var accessedURLs: [URL] = []

    override func loadView() {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .centerX
        root.spacing = 12
        root.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        status.alignment = .center
        status.maximumNumberOfLines = 3
        root.addArrangedSubview(status)
        let cancel = NSButton(title: "Cancel", target: self, action: #selector(cancelAction))
        root.addArrangedSubview(cancel)
        view = root
    }

    override func viewDidAppear() {
        super.viewDidAppear()
        guard !started else { return }
        started = true
        Task { @MainActor in await beginHandoff() }
    }

    @objc private func cancelAction() {
        stopAccessing()
        extensionContext?.cancelRequest(withError: OmniViaExtensionError.invalidInput)
    }

    @MainActor
    private func beginHandoff() async {
        do {
            guard let context = extensionContext else { throw OmniViaExtensionError.invalidInput }
            let originalItems = context.inputItems
            let providers = originalItems
                .compactMap { $0 as? NSExtensionItem }
                .flatMap { $0.attachments ?? [] }
            guard !providers.isEmpty,
                  providers.count <= NativeHandoffMailbox.maximumSlots
            else { throw OmniViaExtensionError.invalidInput }

            let mailbox = try NativeHandoffMailbox.appGroupMailbox()
            let nonce = UUID().uuidString.lowercased()
            var slots: [NativeHandoffSlotV1] = []
            for provider in providers {
                guard provider.hasItemConformingToTypeIdentifier("public.file-url") else {
                    throw OmniViaExtensionError.invalidInput
                }
                let url = try await loadFileURL(from: provider)
                if url.startAccessingSecurityScopedResource() { accessedURLs.append(url) }
                let bookmark = try url.bookmarkData(
                    options: [],
                    includingResourceValuesForKeys: [.isRegularFileKey, .isDirectoryKey],
                    relativeTo: nil
                )
                slots.append(NativeHandoffSlotV1(
                    slotID: UUID().uuidString.lowercased(),
                    displayName: url.lastPathComponent,
                    sourceKind: try Self.sourceKind(of: url),
                    bookmark: bookmark
                ))
            }
            let now = Date()
            let offer = NativeHandoffOfferV1(
                requestNonce: nonce,
                expectedOwnerBundleID: NativeHandoffMailbox.companionBundleID,
                senderBundleID: NativeHandoffMailbox.finderBundleID,
                surface: .finderQuickAction,
                createdAt: now,
                expiresAt: now.addingTimeInterval(NativeHandoffMailbox.maximumLifetime),
                slots: slots
            )
            status.stringValue = "Waiting for OmniVia Core…"
            let acknowledgement = try await submitAuthenticatedOffer(offer, mailbox: mailbox)
            guard NativeHandoffMailbox.adoptionSatisfiesOffer(
                offeredSlotIDs: slots.map(\.slotID),
                acknowledgedSlotIDs: acknowledgement.adoptions.map(\.slotID)
            ) else {
                throw OmniViaExtensionError.invalidInput
            }
            stopAccessing()
            // The items this action was given are returned exactly as they
            // arrived: the same `NSExtensionItem` objects carrying the same
            // providers, unopened and unmodified, so the action is a viewer and
            // changes nothing about the Finder selection.
            context.completeRequest(returningItems: originalItems, completionHandler: nil)
        } catch {
            stopAccessing()
            status.stringValue = "Choose the original in OmniVia Core. You can retry or cancel this action."
        }
    }

    /// What one loaded selection URL is, read from the file system rather than
    /// taken from the item's declared types. The activation rule admits folders
    /// as well as files, so a selection is classified here: a regular file is
    /// offered as an original grant, a plain directory as a folder root. A
    /// package, a symlink, or anything that is neither — a device, a socket, an
    /// item that has gone — is refused, which surfaces as the action's retry
    /// message rather than as a silently narrowed selection.
    private static func sourceKind(of url: URL) throws -> NativeHandoffSourceKind {
        guard let values = try? url.resourceValues(forKeys: [
            .isRegularFileKey,
            .isDirectoryKey,
            .isPackageKey,
            .isSymbolicLinkKey,
        ]) else { throw OmniViaExtensionError.invalidInput }
        if values.isRegularFile == true { return .originalBookmark }
        guard values.isDirectory == true,
              values.isPackage != true,
              values.isSymbolicLink != true
        else { throw OmniViaExtensionError.invalidInput }
        return .folderBookmark
    }

    private func stopAccessing() {
        for url in accessedURLs { url.stopAccessingSecurityScopedResource() }
        accessedURLs.removeAll()
    }
}
