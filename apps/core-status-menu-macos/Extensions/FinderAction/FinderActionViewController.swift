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
                    sourceKind: .originalBookmark,
                    bookmark: bookmark
                ))
            }
            let now = Date()
            try mailbox.writeOffer(NativeHandoffOfferV1(
                requestNonce: nonce,
                expectedOwnerBundleID: NativeHandoffMailbox.companionBundleID,
                senderBundleID: NativeHandoffMailbox.finderBundleID,
                surface: .finderQuickAction,
                createdAt: now,
                expiresAt: now.addingTimeInterval(NativeHandoffMailbox.maximumLifetime),
                slots: slots
            ), now: now)
            try activateContainingApp()
            status.stringValue = "Waiting for OmniVia Core…"
            let acknowledgement = try await waitForAcknowledgement(mailbox: mailbox, nonce: nonce)
            guard acknowledgement.adoptions.map(\.slotID) == slots.map(\.slotID) else {
                throw OmniViaExtensionError.invalidInput
            }
            stopAccessing()
            mailbox.removeAcknowledgement(nonce: nonce)
            context.completeRequest(returningItems: originalItems, completionHandler: nil)
        } catch {
            stopAccessing()
            status.stringValue = "Choose the original in OmniVia Core. You can retry or cancel this action."
        }
    }

    private func stopAccessing() {
        for url in accessedURLs { url.stopAccessingSecurityScopedResource() }
        accessedURLs.removeAll()
    }
}
